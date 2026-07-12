from flask import Blueprint, render_template, request, jsonify

from auth_utils import get_current_user, login_required
from config import Config
from db import get_db
from rag import engine as rag

chat_bp = Blueprint("chat", __name__, url_prefix="/chat")


# ── Conversation helpers ──────────────────────────────────────────────────────


def _get_or_create_conversation(conn, user_id: str) -> str:
    """Return conversation_id (str UUID) for the user, creating one if needed."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
        conn.commit()
        cur.execute(
            "SELECT id::text AS id FROM conversations WHERE user_id = %s", (user_id,)
        )
        row = cur.fetchone()
    return row["id"]


# ── Routes ────────────────────────────────────────────────────────────────────


@chat_bp.route("/")
@login_required
def chat_page():
    user = get_current_user()
    conn = get_db()
    conv_id = _get_or_create_conversation(conn, user["sub"])
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
        cur.execute("SELECT COUNT(*) AS cnt FROM recipes")
        user_recipe_count = cur.fetchone()["cnt"]
    return render_template(
        "chat/index.html",
        messages=messages,
        index_ready=True,
        index_error=None,
        chunk_count=user_recipe_count,
    )


@chat_bp.route("/ask", methods=["POST"])
@login_required
def ask():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()

    if not question:
        return jsonify({"error": "No question provided"}), 400

    conn = get_db()

    # Fetch user pantry
    with conn.cursor() as cur:
        cur.execute("SELECT ingredient FROM pantry WHERE user_id = %s", (user["sub"],))
        pantry = [r["ingredient"] for r in cur.fetchall()]

    # Get (or create) conversation and fetch recent history
    conv_id = _get_or_create_conversation(conn, user["sub"])
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

    # Retrieve recipe chunks from AWS Bedrock Knowledge Base
    recipe_chunks = rag.retrieve_chunks(question, Config.TOP_K)

    # Build context: inject user recipe count info so Chef AI knows about the catalog
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM recipes")
        user_count = cur.fetchone()["cnt"]

    kb_note = (
        f"[Catalog note: The knowledge base contains ~1100 built-in recipes plus "
        f"{user_count} user-added recipe(s). Answer confidently about what's available.]"
    )
    context_chunks = [kb_note] + recipe_chunks

    # Call Nova Lite via Bedrock
    try:
        answer = rag.ask_chef(question, context_chunks, recent, pantry)
    except Exception as exc:
        import traceback

        traceback.print_exc()  # prints full stack trace to server console
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
                        "error": "Chef AI access denied. Check AWS credentials and Bedrock model access."
                    }
                ),
                503,
            )
        if "ValidationException" in err_str:
            # Message format rejected by Converse API — clear history so next request starts fresh
            return (
                jsonify(
                    {"error": f"Chef AI rejected the request format: {err_str[:200]}"}
                ),
                400,
            )
        return jsonify({"error": f"Chef AI error: {err_str[:200]}"}), 500

    # Persist both messages to RDS.
    # Use clock_timestamp() (not NOW()) so each INSERT gets a distinct timestamp
    # even within the same transaction — guarantees correct ordering on reload.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (%s, %s, %s, clock_timestamp())",
            (conv_id, "user", question),
        )
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (%s, %s, %s, clock_timestamp())",
            (conv_id, "assistant", answer),
        )
    conn.commit()

    return jsonify({"answer": answer, "sources": len(recipe_chunks)})


@chat_bp.route("/clear", methods=["POST"])
@login_required
def clear_history():
    user = get_current_user()
    conn = get_db()
    conv_id = _get_or_create_conversation(conn, user["sub"])
    with conn.cursor() as cur:
        cur.execute("DELETE FROM messages WHERE conversation_id = %s", (conv_id,))
    conn.commit()
    return jsonify({"message": "Conversation history cleared"})


@chat_bp.route("/reload-index", methods=["POST"])
@login_required
def reload_index():
    job_id = rag.sync_knowledge_base()
    if job_id:
        return jsonify({"message": f"Knowledge base sync started (job: {job_id})"})
    return jsonify({"error": "Failed to start knowledge base sync"}), 503
