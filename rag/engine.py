"""
RAG engine — AWS Bedrock edition.

Public API:
  retrieve_chunks(question, top_k)  → list[str]   Bedrock KB semantic retrieval
  ask_chef(question, chunks, history, pantry) → str  Nova Lite via Converse API
  parse_recipe_from_text(raw_text)  → dict        Structured recipe extraction
  sync_knowledge_base()             → dict        Trigger KB ingestion job(s)
  get_index_status(conn, user_id)   → dict        unified recipe index summary
"""

import json
import logging
import re
from typing import Optional, TypedDict

import boto3
from botocore.exceptions import ClientError

from config import Config

logger = logging.getLogger(__name__)

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

Grounding rules (critical):
- When answering about a specific cookbook recipe's ingredients, steps, or tags, use ONLY the provided context blocks — especially any block labeled "Authoritative cookbook entry".
- If multiple recipes appear in context, prefer the Authoritative cookbook entry over other excerpts.
- Do not invent, merge, or substitute details from other recipes. If the context does not contain the answer, say you cannot find it in the cookbook.
- For meta-questions about the conversation (e.g. "what was my last question?", "what did I ask before?"), answer from the chat history shown in the messages — not from the knowledge base.

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

Only append the block when you have fully invented the recipe from scratch with no cookbook source.
Always use the exact fence label ```recipe-json (not plain ```json), even in follow-up messages when the user asks for another new recipe:

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

# ── Types ─────────────────────────────────────────────────────────────────────


class SyncResult(TypedDict):
    ok: bool
    job_ids: list[str]
    message: str
    error: str | None


class IndexStatus(TypedDict):
    catalog_count: int
    user_count: int
    total_documents: int
    last_sync_status: str | None


# ── Lazy AWS clients ──────────────────────────────────────────────────────────

_bedrock_runtime = None
_bedrock_agent_runtime = None
_bedrock_agent = None
_s3_client = None


def _clients():
    global _bedrock_runtime, _bedrock_agent_runtime, _bedrock_agent
    if _bedrock_runtime is None:
        session = boto3.Session(region_name=Config.AWS_REGION)
        _bedrock_runtime = session.client("bedrock-runtime")
        _bedrock_agent_runtime = session.client("bedrock-agent-runtime")
        _bedrock_agent = session.client("bedrock-agent")
    return _bedrock_runtime, _bedrock_agent_runtime, _bedrock_agent


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=Config.AWS_REGION)
    return _s3_client


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
    *,
    has_authoritative_context: bool = False,
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
        content = str(msg.get("content", ""))[: Config.HISTORY_MESSAGE_MAX_CHARS]
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

    temperature = 0.3 if has_authoritative_context else 0.7
    response = runtime.converse(
        modelId=Config.NOVA_MODEL_ID,
        system=[{"text": system_text}],
        messages=messages,
        inferenceConfig={"maxTokens": Config.NOVA_MAX_OUTPUT_TOKENS, "temperature": temperature},
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
        inferenceConfig={"maxTokens": Config.NOVA_MAX_OUTPUT_TOKENS, "temperature": 0.1},
    )
    text = response["output"]["message"]["content"][0]["text"].strip()
    # Strip markdown fences if the model wrapped the JSON
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model returned non-JSON: {exc}") from exc


# ── Knowledge Base helpers ────────────────────────────────────────────────────


def _catalog_s3_prefix() -> str:
    return f"{Config.S3_RECIPES_PREFIX}catalog/"


def _data_source_ids_to_sync() -> list[str]:
    """Return Bedrock data source IDs to sync for this KB."""
    if Config.BEDROCK_KB_DS_ID and not Config.BEDROCK_KB_SYNC_ALL:
        return [Config.BEDROCK_KB_DS_ID]
    if not Config.BEDROCK_KB_ID:
        return []

    _, _, agent = _clients()
    ids: list[str] = []
    kwargs: dict = {"knowledgeBaseId": Config.BEDROCK_KB_ID}
    while True:
        response = agent.list_data_sources(**kwargs)
        for summary in response.get("dataSourceSummaries", []):
            ds_id = summary.get("dataSourceId")
            if ds_id:
                ids.append(ds_id)
        next_token = response.get("nextToken")
        if not next_token:
            break
        kwargs["nextToken"] = next_token
    return ids


def _get_in_progress_job_id(agent, data_source_id: str) -> str | None:
    response = agent.list_ingestion_jobs(
        knowledgeBaseId=Config.BEDROCK_KB_ID,
        dataSourceId=data_source_id,
        maxResults=5,
        sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
    )
    for job in response.get("ingestionJobSummaries", []):
        if job.get("status") in ("STARTING", "IN_PROGRESS"):
            return job.get("ingestionJobId")
    return None


def _start_ingestion_for_ds(agent, data_source_id: str) -> tuple[str | None, str]:
    """Start ingestion for one data source. Returns (job_id, message)."""
    try:
        response = agent.start_ingestion_job(
            knowledgeBaseId=Config.BEDROCK_KB_ID,
            dataSourceId=data_source_id,
        )
        job_id = response.get("ingestionJob", {}).get("ingestionJobId")
        return job_id, f"Sync started for data source {data_source_id}"
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConflictException":
            job_id = _get_in_progress_job_id(agent, data_source_id)
            if job_id:
                return job_id, f"Sync already in progress for data source {data_source_id}"
            return None, f"Sync already in progress for data source {data_source_id}"
        raise


def sync_knowledge_base() -> SyncResult:
    """
    Trigger Bedrock Knowledge Base ingestion job(s) so newly uploaded recipes
    become searchable. Returns a structured result with job IDs and errors.
    """
    if not Config.BEDROCK_KB_ID:
        return {
            "ok": False,
            "job_ids": [],
            "message": "Knowledge base sync failed",
            "error": "BEDROCK_KB_ID is not configured",
        }

    data_source_ids = _data_source_ids_to_sync()
    if not data_source_ids:
        return {
            "ok": False,
            "job_ids": [],
            "message": "Knowledge base sync failed",
            "error": "No data sources found. Set BEDROCK_KB_DS_ID or BEDROCK_KB_SYNC_ALL=true",
        }

    _, _, agent = _clients()
    job_ids: list[str] = []
    messages: list[str] = []
    errors: list[str] = []

    for ds_id in data_source_ids:
        try:
            job_id, message = _start_ingestion_for_ds(agent, ds_id)
            if job_id:
                job_ids.append(job_id)
            messages.append(message)
        except ClientError as exc:
            err_msg = exc.response.get("Error", {}).get("Message", str(exc))
            logger.exception("Failed to start ingestion for data source %s", ds_id)
            errors.append(f"{ds_id}: {err_msg}")
        except Exception as exc:
            logger.exception("Failed to start ingestion for data source %s", ds_id)
            errors.append(f"{ds_id}: {exc}")

    if job_ids:
        summary = "; ".join(messages)
        if len(job_ids) == 1:
            user_message = f"Knowledge base sync started (job: {job_ids[0]})"
        else:
            user_message = f"Knowledge base sync started ({len(job_ids)} jobs: {', '.join(job_ids)})"
        if "already in progress" in summary.lower():
            user_message = summary
        return {
            "ok": True,
            "job_ids": job_ids,
            "message": user_message,
            "error": "; ".join(errors) if errors else None,
        }

    return {
        "ok": False,
        "job_ids": [],
        "message": "Failed to start knowledge base sync",
        "error": "; ".join(errors) if errors else "Unknown error",
    }


def _count_s3_md_files(prefix: str) -> int:
    if not Config.S3_BUCKET:
        return 0
    count = 0
    paginator = _s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=Config.S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj.get("Key", "").endswith(".md"):
                count += 1
    return count


def _get_last_sync_status() -> str | None:
    if not Config.BEDROCK_KB_ID:
        return None

    data_source_ids = _data_source_ids_to_sync()
    if not data_source_ids:
        return None

    _, _, agent = _clients()
    statuses: list[str] = []
    for ds_id in data_source_ids:
        try:
            response = agent.list_ingestion_jobs(
                knowledgeBaseId=Config.BEDROCK_KB_ID,
                dataSourceId=ds_id,
                maxResults=1,
                sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
            )
            jobs = response.get("ingestionJobSummaries", [])
            if jobs:
                statuses.append(jobs[0].get("status", "UNKNOWN"))
        except Exception:
            logger.exception("Failed to fetch ingestion status for data source %s", ds_id)
    if not statuses:
        return None
    if len(statuses) == 1:
        return statuses[0]
    return ", ".join(statuses)


def get_index_status(conn=None, user_id: str | None = None) -> IndexStatus:
    """
    Return indexed document counts plus the latest Bedrock ingestion job status.

    Counts are derived from the same unified recipe index used by the main recipe
    page (``s3_recipes.build_recipe_index``) so both pages always agree. When no DB
    connection is available, falls back to a raw S3 catalog count.
    """
    catalog_count = 0
    user_count = 0
    if conn is not None:
        from services import s3_recipes

        index = s3_recipes.build_recipe_index(conn, user_id=user_id)
        for entry in index:
            if entry.get("source") == "user":
                user_count += 1
            else:
                catalog_count += 1
    else:
        catalog_count = _count_s3_md_files(_catalog_s3_prefix())

    return {
        "catalog_count": catalog_count,
        "user_count": user_count,
        "total_documents": catalog_count + user_count,
        "last_sync_status": _get_last_sync_status(),
    }
