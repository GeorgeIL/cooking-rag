import json
import re

import boto3
from flask import Blueprint, jsonify, render_template, request

from auth_utils import get_current_user, login_required
from config import Config
from db import get_db
from services import s3_recipes

buddies_bp = Blueprint("buddies", __name__, url_prefix="/buddies")

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _validate_buddy_fields(name: str, email: str, picture_url: str) -> str | None:
    if not name:
        return "Name is required"
    if len(name) > 100:
        return "Name is too long (max 100 characters)"
    if not email:
        return "Email is required"
    if len(email) > 255:
        return "Email is too long"
    if not _EMAIL_RE.match(email):
        return "Invalid email address"
    if picture_url and len(picture_url) > 1000:
        return "Picture URL is too long"
    if picture_url and not picture_url.startswith(("http://", "https://")):
        return "Picture URL must start with http:// or https://"
    return None


def _row_to_buddy(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "picture_url": row["picture_url"] or "",
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _fetch_buddies(conn, user_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text AS id, name, email, picture_url, created_at, updated_at
            FROM cooking_buddies
            WHERE user_id = %s
            ORDER BY name
            """,
            (user_id,),
        )
        return [_row_to_buddy(dict(r)) for r in cur.fetchall()]


def _get_buddy(conn, user_id: str, buddy_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text AS id, name, email, picture_url, created_at, updated_at
            FROM cooking_buddies
            WHERE id = %s AND user_id = %s
            """,
            (buddy_id, user_id),
        )
        row = cur.fetchone()
    return _row_to_buddy(dict(row)) if row else None


@buddies_bp.route("/")
@login_required
def buddies_page():
    user = get_current_user()
    conn = get_db()
    buddies = _fetch_buddies(conn, user["sub"])
    return render_template("buddies/index.html", buddies=buddies)


@buddies_bp.route("/list", methods=["GET"])
@login_required
def list_buddies():
    user = get_current_user()
    conn = get_db()
    return jsonify({"buddies": _fetch_buddies(conn, user["sub"])})


@buddies_bp.route("/recipes", methods=["GET"])
@login_required
def list_share_recipes():
    user = get_current_user()
    conn = get_db()
    query = (request.args.get("q") or "").strip()
    try:
        limit = min(int(request.args.get("limit", 50)), 100)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "Invalid pagination parameters"}), 400

    _, recipes, total = s3_recipes.search_recipes(
        conn, query, limit, offset, user_id=user["sub"]
    )
    return jsonify({"recipes": recipes, "total": total})


@buddies_bp.route("/recipes/<path:s3_key>", methods=["GET"])
@login_required
def get_share_recipe(s3_key: str):
    user = get_current_user()
    if not s3_recipes.is_valid_recipe_key(s3_key):
        return jsonify({"error": "Invalid recipe key"}), 400
    slug = s3_key.rsplit("/", 1)[-1][:-3] if s3_key.endswith(".md") else ""
    conn = get_db()
    if not s3_recipes.user_can_access_recipe(conn, user["sub"], slug, s3_key):
        return jsonify({"error": "Recipe not found"}), 404
    try:
        preview = s3_recipes.get_recipe_preview(s3_key)
    except Exception as exc:
        return jsonify({"error": f"Recipe not found: {exc}"}), 404
    return jsonify(preview)


@buddies_bp.route("/send", methods=["POST"])
@login_required
def send_to_buddies():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    buddy_ids = data.get("buddy_ids") or []
    subject = (data.get("subject") or "").strip()
    context = (data.get("context") or "").strip()
    s3_key = (data.get("s3_key") or "").strip()
    personal_note = (data.get("personal_note") or "").strip()

    if not buddy_ids or not isinstance(buddy_ids, list):
        return jsonify({"error": "Select at least one buddy"}), 400
    if not Config.BUDDY_EMAIL_LAMBDA_NAME:
        return jsonify({"error": "Email service is not configured"}), 503

    if s3_key:
        if not s3_recipes.is_valid_recipe_key(s3_key):
            return jsonify({"error": "Invalid recipe selected"}), 400
        conn = get_db()
        slug = s3_key.rsplit("/", 1)[-1][:-3] if s3_key.endswith(".md") else ""
        if not s3_recipes.user_can_access_recipe(conn, user["sub"], slug, s3_key):
            return jsonify({"error": "Recipe not found"}), 404
        try:
            md_content = s3_recipes.get_recipe_content(s3_key)
            context = s3_recipes.build_email_context(md_content, personal_note)
            if not subject:
                subject = f"Recipe: {s3_recipes.title_for_key(s3_key, md_content)}"
        except Exception as exc:
            return jsonify({"error": f"Failed to load recipe: {exc}"}), 404
    elif not context:
        return jsonify({"error": "Select a recipe or add a message"}), 400

    conn = get_db()
    placeholders = ",".join(["%s"] * len(buddy_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id::text AS id, name, email
            FROM cooking_buddies
            WHERE user_id = %s AND id IN ({placeholders})
            """,
            [user["sub"], *buddy_ids],
        )
        buddies = [dict(r) for r in cur.fetchall()]

    if not buddies:
        return jsonify({"error": "No valid buddies selected"}), 400

    payload = {
        "sender_name": user.get("username") or "A Smart Cookbook user",
        "subject": subject or "A recipe from Smart Cookbook",
        "context": context,
        "buddies": [{"name": b["name"], "email": b["email"]} for b in buddies],
    }

    try:
        client = boto3.client("lambda", region_name=Config.AWS_REGION)
        client.invoke(
            FunctionName=Config.BUDDY_EMAIL_LAMBDA_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except Exception as exc:
        return jsonify({"error": f"Failed to send email: {exc}"}), 503

    return jsonify(
        {
            "message": f"Email queued for {len(buddies)} buddy(s)",
            "count": len(buddies),
        }
    )


@buddies_bp.route("/", methods=["POST"])
@login_required
def create_buddy():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    picture_url = (data.get("picture_url") or "").strip()

    error = _validate_buddy_fields(name, email, picture_url)
    if error:
        return jsonify({"error": error}), 400

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cooking_buddies (user_id, name, email, picture_url)
                VALUES (%s, %s, %s, %s)
                RETURNING id::text AS id, name, email, picture_url, created_at, updated_at
                """,
                (user["sub"], name, email, picture_url),
            )
            row = dict(cur.fetchone())
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if "cooking_buddies_user_id_email_key" in str(exc):
            return jsonify({"error": "A buddy with this email already exists"}), 409
        raise

    return jsonify({"message": "Buddy added", "buddy": _row_to_buddy(row)}), 201


@buddies_bp.route("/<buddy_id>", methods=["PUT"])
@login_required
def update_buddy(buddy_id: str):
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    picture_url = (data.get("picture_url") or "").strip()

    error = _validate_buddy_fields(name, email, picture_url)
    if error:
        return jsonify({"error": error}), 400

    conn = get_db()
    if not _get_buddy(conn, user["sub"], buddy_id):
        return jsonify({"error": "Buddy not found"}), 404

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE cooking_buddies
                SET name = %s, email = %s, picture_url = %s, updated_at = NOW()
                WHERE id = %s AND user_id = %s
                RETURNING id::text AS id, name, email, picture_url, created_at, updated_at
                """,
                (name, email, picture_url, buddy_id, user["sub"]),
            )
            row = dict(cur.fetchone())
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if "cooking_buddies_user_id_email_key" in str(exc):
            return jsonify({"error": "A buddy with this email already exists"}), 409
        raise

    return jsonify({"message": "Buddy updated", "buddy": _row_to_buddy(row)})


@buddies_bp.route("/<buddy_id>", methods=["DELETE"])
@login_required
def delete_buddy(buddy_id: str):
    user = get_current_user()
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM cooking_buddies WHERE id = %s AND user_id = %s RETURNING id",
            (buddy_id, user["sub"]),
        )
        deleted = cur.fetchone()
    conn.commit()
    if not deleted:
        return jsonify({"error": "Buddy not found"}), 404
    return jsonify({"message": "Buddy deleted"})
