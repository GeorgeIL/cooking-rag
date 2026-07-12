import re

from flask import (
    Blueprint,
    make_response,
    redirect,
    render_template,
    request,
    jsonify,
    url_for,
)

from auth_utils import check_password, create_token, get_current_user, hash_password
from db import get_db

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


# ── Login ─────────────────────────────────────────────────────────────────────


@auth_bp.route("/login")
def login_page():
    if get_current_user():
        return redirect(url_for("home"))
    return render_template("auth/login.html")


@auth_bp.route("/login", methods=["POST"])
def login_post():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text AS id, username, password_hash FROM users WHERE email = %s",
            (email,),
        )
        user = cur.fetchone()

    if not user or not check_password(password, user["password_hash"]):
        return jsonify({"error": "Invalid email or password"}), 401

    token = create_token(user["id"], user["username"])
    resp = make_response(
        jsonify(
            {
                "message": "Login successful",
                "username": user["username"],
                "redirect": url_for("home"),
            }
        )
    )
    resp.set_cookie("token", token, httponly=True, samesite="Lax", max_age=86400)
    return resp


# ── Sign-up ───────────────────────────────────────────────────────────────────


@auth_bp.route("/signup")
def signup_page():
    if get_current_user():
        return redirect(url_for("home"))
    return render_template("auth/signup.html")


@auth_bp.route("/signup", methods=["POST"])
def signup_post():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not username or not email or not password:
        return jsonify({"error": "All fields are required"}), 400
    if not (3 <= len(username) <= 30):
        return jsonify({"error": "Username must be 3–30 characters"}), 400
    if not re.fullmatch(r"[a-zA-Z0-9_]+", username):
        return (
            jsonify(
                {"error": "Username may only contain letters, numbers and underscores"}
            ),
            400,
        )
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if not re.fullmatch(r"[\w.\-+]+@[\w.\-]+\.\w{2,}", email):
        return jsonify({"error": "Invalid email address"}), 400

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cur.fetchone():
            return jsonify({"error": "Email already registered"}), 409
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        if cur.fetchone():
            return jsonify({"error": "Username already taken"}), 409
        cur.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s) RETURNING id::text AS id",
            (username, email, hash_password(password)),
        )
        row = cur.fetchone()
        user_id = row["id"]
    conn.commit()

    token = create_token(user_id, username)
    resp = make_response(
        jsonify(
            {
                "message": "Account created successfully",
                "username": username,
                "redirect": url_for("home"),
            }
        ),
        201,
    )
    resp.set_cookie("token", token, httponly=True, samesite="Lax", max_age=86400)
    return resp


# ── Logout ────────────────────────────────────────────────────────────────────


@auth_bp.route("/logout", methods=["POST"])
def logout():
    resp = make_response(redirect(url_for("auth.login_page")))
    resp.delete_cookie("token")
    return resp


# ── Current user (API) ────────────────────────────────────────────────────────


@auth_bp.route("/me")
def me():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"user_id": user["sub"], "username": user["username"]})
