"""
RAG engine — AWS Bedrock edition.

Public API:
  retrieve_chunks(question, top_k)  → list[str]   Bedrock KB semantic retrieval
  ask_chef(question, chunks, history, pantry) → str  Nova Lite via Converse API
  parse_recipe_from_text(raw_text)  → dict        Structured recipe extraction
  sync_knowledge_base()             → str | None  Trigger KB ingestion job
"""

import json
import re
from typing import Optional

import boto3

from config import Config

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Chef AI, a knowledgeable and warm culinary assistant for a smart cookbook app.

Your capabilities:
- Help users find recipes based on ingredients they currently have at home
- Suggest practical ingredient substitutions when something is missing
- Explain cooking techniques, methods, and food science
- Recommend recipes based on dietary needs, cuisine type, or occasion
- Answer questions about food storage, nutrition, and kitchen tips

Guidelines:
- Be encouraging and enthusiastic about cooking
- Reference specific recipes from the cookbook when relevant
- Always suggest substitutions when asked - be creative and practical
- If a user lists their pantry items, tailor recommendations to those ingredients
- Format lists and steps clearly using markdown (bold headings, bullet points, numbered steps)
- Mention estimated cooking times and difficulty when helpful
- Be concise but thorough; avoid unnecessary filler

New Recipe Format:
You MUST append the recipe-json block ONLY in this one specific situation:
  → The user explicitly asked you to CREATE or INVENT a new recipe, AND you are writing out a complete recipe (title + ingredients + steps) that does NOT already exist in the cookbook.

Do NOT include the recipe-json block in ANY of these situations:
- You are describing, summarising, or explaining a recipe that already exists in the cookbook or knowledge base.
- The retrieved cookbook context contains the recipe you are presenting.
- You are recommending, listing, or referencing named recipes.
- You are answering a question about cooking technique, substitutions, storage, or nutrition.
- You are adapting or tweaking an existing recipe (e.g. "make it vegan") — that is still an existing recipe.
- The user asked "what can I make" or "show me recipes" — those are recommendations, not creations.

Only append the block when you have fully invented the recipe from scratch with no cookbook source:

```recipe-json
{"title": "Recipe Name", "description": "One sentence description", "ingredients": ["quantity ingredient", "quantity ingredient"], "steps": ["First instruction", "Second instruction"], "notes": "Optional tips, or empty string", "tags": ["tag1", "tag2"]}
```

Strict rules for the recipe-json block:
- One occurrence maximum per response.
- The JSON must be on a single line with no line breaks inside.
- All array values must be plain strings (no nested objects).
- If in doubt, omit it."""

_PARSE_PROMPT = (
    "Extract the recipe from the text below and return ONLY a valid JSON object "
    "with these exact keys (no markdown fences, no explanation):\n"
    '{"title": "...", "description": "...", "ingredients": ["..."], '
    '"steps": ["..."], "notes": "...", "tags": ["..."]}\n\n'
    "Recipe text:\n"
)

# ── Lazy AWS clients ──────────────────────────────────────────────────────────

_bedrock_runtime = None
_bedrock_agent_runtime = None
_bedrock_agent = None


def _clients():
    global _bedrock_runtime, _bedrock_agent_runtime, _bedrock_agent
    if _bedrock_runtime is None:
        session = boto3.Session(region_name=Config.AWS_REGION)
        _bedrock_runtime = session.client("bedrock-runtime")
        _bedrock_agent_runtime = session.client("bedrock-agent-runtime")
        _bedrock_agent = session.client("bedrock-agent")
    return _bedrock_runtime, _bedrock_agent_runtime, _bedrock_agent


# ── Retrieval ─────────────────────────────────────────────────────────────────


def retrieve_chunks(question: str, top_k: Optional[int] = None) -> list[str]:
    """Retrieve the most relevant text chunks from the Bedrock Knowledge Base."""
    top_k = top_k or Config.TOP_K
    _, agent_rt, _ = _clients()
    try:
        response = agent_rt.retrieve(
            knowledgeBaseId=Config.BEDROCK_KB_ID,
            retrievalQuery={"text": question},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": top_k}
            },
        )
        return [
            r.get("content", {}).get("text", "").strip()
            for r in response.get("retrievalResults", [])
            if r.get("content", {}).get("text", "").strip()
        ]
    except Exception:
        return []


# ── Chef AI (Nova Lite via Converse API) ──────────────────────────────────────


def ask_chef(
    question: str,
    context_chunks: list[str],
    recent_history: list[dict],
    pantry: list[str] | None = None,
) -> str:
    """
    Call Nova Lite 1.0 via the Bedrock Converse API.
    - context_chunks: retrieved KB passages injected into the user turn
    - recent_history: last N messages as [{role, content}, ...]
    - pantry: user's ingredient list added to the system prompt
    """
    runtime, _, _ = _clients()

    system_text = SYSTEM_PROMPT
    if pantry:
        system_text += (
            f"\n\nThe user currently has these ingredients in their pantry: "
            f"{', '.join(pantry)}. Prioritise recipes and suggestions using these ingredients."
        )

    # Build conversation history for the Converse API.
    # Rules: must start with "user", must alternate user/assistant.
    messages: list[dict] = []
    for msg in recent_history:
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        # Enforce alternation: skip if same role as the last added message
        if messages and messages[-1]["role"] == role:
            continue
        content = str(msg.get("content", ""))[:500]
        messages.append({"role": role, "content": [{"text": content}]})

    # Converse API requires first message to be "user"; drop leading assistant turns
    while messages and messages[0]["role"] == "assistant":
        messages.pop(0)

    # Inject retrieved context into the current user turn
    context_text = "\n\n---\n\n".join(context_chunks) if context_chunks else ""
    if context_text:
        user_content = f"Relevant cookbook context:\n{context_text}\n\n---\n\nUser question: {question}"
    else:
        user_content = question

    messages.append({"role": "user", "content": [{"text": user_content}]})

    response = runtime.converse(
        modelId=Config.NOVA_MODEL_ID,
        system=[{"text": system_text}],
        messages=messages,
        inferenceConfig={"maxTokens": 1024, "temperature": 0.7},
    )
    return response["output"]["message"]["content"][0]["text"]


# ── Recipe parser ─────────────────────────────────────────────────────────────


def parse_recipe_from_text(raw_text: str) -> dict:
    """
    Use Nova Lite to extract structured recipe data from raw PDF/TXT content.
    Returns dict with keys: title, description, ingredients, steps, notes, tags.
    Raises ValueError if the response is not valid JSON.
    """
    runtime, _, _ = _clients()
    prompt = _PARSE_PROMPT + raw_text[:4000]

    response = runtime.converse(
        modelId=Config.NOVA_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 1024, "temperature": 0.1},
    )
    text = response["output"]["message"]["content"][0]["text"].strip()
    # Strip markdown fences if the model wrapped the JSON
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model returned non-JSON: {exc}") from exc


# ── Knowledge Base sync ───────────────────────────────────────────────────────


def sync_knowledge_base() -> str | None:
    """
    Trigger a Bedrock Knowledge Base ingestion job so newly uploaded recipes
    become searchable. Returns the job ID or None on failure.
    Ingestion typically completes in 30 seconds to a few minutes.
    """
    _, _, agent = _clients()
    try:
        response = agent.start_ingestion_job(
            knowledgeBaseId=Config.BEDROCK_KB_ID,
            dataSourceId=Config.BEDROCK_KB_DS_ID,
        )
        return response.get("ingestionJob", {}).get("ingestionJobId")
    except Exception:
        return None
