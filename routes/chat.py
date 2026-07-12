import json

import boto3
from flask import Blueprint, render_template, request, jsonify

from auth_utils import get_current_user, login_required
from config import Config
from db import get_db
from rag import engine as rag
from services import bedrock_agent, buddy_share, recipe_from_chat, recipe_lookup

# Max chars of last recipe body passed to the agent for ShareRecipeWithBuddy
_LAST_RECIPE_PROMPT_MAX_CHARS = 6000

chat_bp = Blueprint("chat", __name__, url_prefix="/chat")


# ── Conversation helpers ──────────────────────────────────────────────────────


def _get_or_create_conversation(conn, user_id: str) -> dict:
    """Return conversation row (id + agent_session_id), creating one if needed."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
        conn.commit()
        cur.execute(
            """
            SELECT id::text AS id, agent_session_id::text AS agent_session_id
            FROM conversations
            WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
    return dict(row)


def _reset_agent_session(conn, conversation_id: str) -> str:
    """Rotate Bedrock agent session (clears poisoned tool/memory state)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE conversations
            SET agent_session_id = gen_random_uuid()
            WHERE id = %s
            RETURNING agent_session_id::text AS agent_session_id
            """,
            (conversation_id,),
        )
        row = cur.fetchone()
    conn.commit()
    return row["agent_session_id"]


def _fetch_buddy_names(conn, user_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name FROM cooking_buddies
            WHERE user_id = %s
            ORDER BY name
            """,
            (user_id,),
        )
        return [row["name"] for row in cur.fetchall()]


def _fetch_buddy_contacts(conn, user_id: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, email FROM cooking_buddies
            WHERE user_id = %s
            ORDER BY name
            """,
            (user_id,),
        )
        return {row["name"]: row["email"] for row in cur.fetchall()}


def _build_email_context_from_body(recipe_title: str, recipe_body: str) -> str:
    title = recipe_title.strip()
    body = recipe_body.strip()
    if body.lower().startswith("#"):
        return body
    return f"# {title}\n\n{body}"


def _lookup_buddy_row(conn, user_id: str, buddy_name: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text AS id, name, email
            FROM cooking_buddies
            WHERE user_id = %s
            ORDER BY name
            """,
            (user_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return None

    resolved = buddy_share.resolve_buddy_name(
        buddy_name.strip(), [row["name"] for row in rows]
    )
    if not resolved:
        return None

    for row in rows:
        if row["name"] == resolved:
            return row
    return None


def _send_recipe_email_to_buddy(
    conn,
    *,
    user_id: str,
    sender_name: str,
    buddy_name: str,
    recipe_title: str,
    recipe_body: str,
) -> tuple[bool, str]:
    if not Config.BUDDY_EMAIL_LAMBDA_NAME:
        return False, "Email service is not configured"

    buddy = _lookup_buddy_row(conn, user_id, buddy_name)

    if not buddy:
        return False, f"No cooking buddy named '{buddy_name}' was found"

    context = _build_email_context_from_body(recipe_title, recipe_body)
    subject = f"Recipe: {recipe_title.strip()}"
    payload = {
        "sender_name": sender_name or "A Smart Cookbook user",
        "subject": subject,
        "context": context,
        "buddies": [{"name": buddy["name"], "email": buddy["email"]}],
    }

    try:
        client = boto3.client("lambda", region_name=Config.AWS_REGION)
        client.invoke(
            FunctionName=Config.BUDDY_EMAIL_LAMBDA_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except Exception as exc:
        return False, f"Failed to send email: {exc}"

    return True, f"Email queued for {buddy['name']} ({buddy['email']})"


def _chef_error_response(exc: Exception):
    err_str = str(exc)
    if "ThrottlingException" in err_str or "throttling" in err_str.lower():
        return (
            jsonify(
                {
                    "error": "Chef AI is busy right now — please wait a few seconds and try again."
                }
            ),
            429,
        )
    if "AccessDeniedException" in err_str:
        return (
            jsonify(
                {
                    "error": "Chef AI access denied. Check AWS credentials, agent ID, and IAM permissions."
                }
            ),
            503,
        )
    if "ValidationException" in err_str:
        return (
            jsonify({"error": f"Chef AI rejected the request: {err_str[:200]}"}),
            400,
        )
    return jsonify({"error": f"Chef AI error: {err_str[:200]}"}), 500


# ── Routes ────────────────────────────────────────────────────────────────────


@chat_bp.route("/")
@login_required
def chat_page():
    user = get_current_user()
    conn = get_db()
    conv = _get_or_create_conversation(conn, user["sub"])
    conv_id = conv["id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role, content FROM ("
            "  SELECT role, content, created_at FROM messages"
            "  WHERE conversation_id = %s"
            "  ORDER BY created_at DESC LIMIT 40"
            ") sub ORDER BY created_at ASC",
            (conv_id,),
        )
        messages = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT title FROM recipes WHERE author_id = %s", (user["sub"],)
        )
        saved_titles = [r["title"] for r in cur.fetchall() if r["title"]]
    index_status = rag.get_index_status(conn, user["sub"])
    return render_template(
        "chat/index.html",
        messages=messages,
        index_ready=True,
        index_error=None,
        index_status=index_status,
        saved_titles=saved_titles,
    )


@chat_bp.route("/ask", methods=["POST"])
@login_required
def ask():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()

    if not question:
        return jsonify({"error": "No question provided"}), 400

    if not bedrock_agent.is_agent_configured():
        return (
            jsonify(
                {
                    "error": "Chef AI agent is not configured. Set BEDROCK_AGENT_ID and BEDROCK_AGENT_ALIAS_ID."
                }
            ),
            503,
        )

    conn = get_db()

    with conn.cursor() as cur:
        cur.execute("SELECT ingredient FROM pantry WHERE user_id = %s", (user["sub"],))
        pantry = [r["ingredient"] for r in cur.fetchall()]

    conv = _get_or_create_conversation(conn, user["sub"])
    conv_id = conv["id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role, content FROM ("
            "  SELECT role, content, created_at FROM messages"
            "  WHERE conversation_id = %s"
            "  ORDER BY created_at DESC LIMIT %s"
            ") sub ORDER BY created_at ASC",
            (conv_id, Config.HISTORY_MESSAGES),
        )
        recent = [dict(r) for r in cur.fetchall()]

    active_slugs = recipe_lookup.resolve_active_recipe_slugs(
        question, recent, conn, user_id=user["sub"]
    )
    authoritative = recipe_lookup.build_authoritative_context(
        active_slugs, conn, user_id=user["sub"]
    )
    buddy_names = _fetch_buddy_names(conn, user["sub"])
    buddy_contacts = _fetch_buddy_contacts(conn, user["sub"])
    last_recipe = buddy_share.extract_recipe_from_history(recent, conn, user_id=user["sub"])
    last_recipe_title = ""
    last_recipe_body = ""
    if last_recipe:
        last_recipe_title, last_recipe_body = last_recipe
        last_recipe_body = last_recipe_body[:_LAST_RECIPE_PROMPT_MAX_CHARS]

    sender_name = user.get("username") or "A Smart Cookbook user"

    session_attributes = {
        "user_id": user["sub"],
        "sender_name": sender_name,
        "username": user.get("username") or "",
        "buddy_contacts": json.dumps(buddy_contacts),
    }
    prompt_attributes = {
        "pantry": ", ".join(pantry) if pantry else "none listed",
        "buddy_names": ", ".join(buddy_names) if buddy_names else "none added yet",
        "active_recipe": "\n\n".join(authoritative) if authoritative else "",
        "last_recipe_title": last_recipe_title,
        "last_recipe_body": last_recipe_body,
    }

    try:
        answer = bedrock_agent.invoke_chef_agent(
            question,
            conv["agent_session_id"],
            session_attributes,
            prompt_attributes,
        )
    except Exception as exc:
        import traceback

        traceback.print_exc()
        return _chef_error_response(exc)

    display_answer, saveable_recipe = recipe_from_chat.process_answer(
        answer, active_slugs, conn, user_id=user["sub"]
    )

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (%s, %s, %s, clock_timestamp())",
            (conv_id, "user", question),
        )
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (%s, %s, %s, clock_timestamp())",
            (conv_id, "assistant", display_answer),
        )
    conn.commit()

    payload = {"answer": display_answer, "sources": 0}
    if saveable_recipe:
        payload["recipe"] = saveable_recipe

    return jsonify(payload)


@chat_bp.route("/agent/share-recipe", methods=["POST"])
def agent_share_recipe():
    """Secured endpoint invoked by the Bedrock action Lambda."""
    secret = request.headers.get("X-Agent-Tool-Secret", "")
    if not Config.AGENT_TOOL_SECRET or secret != Config.AGENT_TOOL_SECRET:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "").strip()
    sender_name = (data.get("sender_name") or "A Smart Cookbook user").strip()
    buddy_name = (data.get("buddy_name") or "").strip()
    recipe_title = (data.get("recipe_title") or "").strip()
    recipe_body = (data.get("recipe_body") or "").strip()

    if not user_id:
        return jsonify({"ok": False, "error": "user_id is required"}), 400
    if not buddy_name:
        return jsonify({"ok": False, "error": "buddy_name is required"}), 400
    if not recipe_title:
        return jsonify({"ok": False, "error": "recipe_title is required"}), 400
    if not recipe_body:
        return jsonify({"ok": False, "error": "recipe_body is required"}), 400

    conn = get_db()
    ok, message = _send_recipe_email_to_buddy(
        conn,
        user_id=user_id,
        sender_name=sender_name,
        buddy_name=buddy_name,
        recipe_title=recipe_title,
        recipe_body=recipe_body,
    )
    if not ok:
        return jsonify({"ok": False, "error": message}), 400

    return jsonify({"ok": True, "message": message})


@chat_bp.route("/clear", methods=["POST"])
@login_required
def clear_history():
    user = get_current_user()
    conn = get_db()
    conv = _get_or_create_conversation(conn, user["sub"])
    conv_id = conv["id"]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM messages WHERE conversation_id = %s", (conv_id,))
    _reset_agent_session(conn, conv_id)
    return jsonify({"message": "Conversation history cleared"})
