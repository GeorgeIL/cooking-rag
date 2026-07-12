from flask import Blueprint, render_template, request, jsonify

from auth_utils import get_current_user, login_required
from db import get_db

pantry_bp = Blueprint("pantry", __name__, url_prefix="/pantry")


@pantry_bp.route("/")
@login_required
def pantry_page():
    user = get_current_user()
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ingredient FROM pantry WHERE user_id = %s ORDER BY ingredient",
            (user["sub"],),
        )
        pantry = [r["ingredient"] for r in cur.fetchall()]
    return render_template("pantry/index.html", pantry=pantry)


@pantry_bp.route("/ingredients", methods=["GET"])
@login_required
def get_ingredients():
    user = get_current_user()
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ingredient FROM pantry WHERE user_id = %s ORDER BY ingredient",
            (user["sub"],),
        )
        pantry = [r["ingredient"] for r in cur.fetchall()]
    return jsonify({"ingredients": pantry})


@pantry_bp.route("/ingredients", methods=["POST"])
@login_required
def add_ingredient():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    ingredient = (data.get("ingredient") or "").strip().lower()

    if not ingredient:
        return jsonify({"error": "Ingredient name is required"}), 400
    if len(ingredient) > 100:
        return jsonify({"error": "Ingredient name is too long"}), 400

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pantry (user_id, ingredient) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user["sub"], ingredient),
        )
    conn.commit()
    return jsonify({"message": f"Added '{ingredient}' to your pantry"})


@pantry_bp.route("/ingredients/<path:ingredient>", methods=["DELETE"])
@login_required
def remove_ingredient(ingredient: str):
    user = get_current_user()
    ingredient = ingredient.strip().lower()
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pantry WHERE user_id = %s AND ingredient = %s",
            (user["sub"], ingredient),
        )
    conn.commit()
    return jsonify({"message": f"Removed '{ingredient}' from your pantry"})


@pantry_bp.route("/clear", methods=["POST"])
@login_required
def clear_pantry():
    user = get_current_user()
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pantry WHERE user_id = %s", (user["sub"],))
    conn.commit()
    return jsonify({"message": "Pantry cleared"})
