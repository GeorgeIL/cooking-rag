from flask import Flask, redirect, render_template, url_for
import os

from auth_utils import get_current_user
from config import Config
from db import close_db, init_schema
from routes.auth import auth_bp
from routes.chat import chat_bp
from routes.buddies import buddies_bp
from routes.pantry import pantry_bp
from routes.recipes import recipes_bp
from services import recipe_images

app = Flask(__name__)
app.config.from_object(Config)

if not Config.BEDROCK_KB_ID:
    print("WARNING: BEDROCK_KB_ID is not set — knowledge base sync and retrieval will fail.")
if not Config.BEDROCK_KB_DS_ID and not Config.BEDROCK_KB_SYNC_ALL:
    print(
        "WARNING: BEDROCK_KB_DS_ID is not set and BEDROCK_KB_SYNC_ALL is false — "
        "sync will attempt to list all data sources when triggered."
    )
if not Config.BUDDY_EMAIL_LAMBDA_NAME:
    print(
        "WARNING: BUDDY_EMAIL_LAMBDA_NAME is not set — buddy recipe emails will fail."
    )
if not Config.BEDROCK_AGENT_ID or not Config.BEDROCK_AGENT_ALIAS_ID:
    print(
        "WARNING: BEDROCK_AGENT_ID / BEDROCK_AGENT_ALIAS_ID not set — Chef AI chat will fail."
    )
if not Config.AGENT_TOOL_SECRET:
    print(
        "WARNING: AGENT_TOOL_SECRET is not set — agent share-recipe tool endpoint will reject calls."
    )
if not Config.GEMINI_API_KEY:
    print(
        "WARNING: GEMINI_API_KEY is not set — async recipe image generation is disabled."
    )

# ── Blueprints ────────────────────────────────────────────────────────────────
app.register_blueprint(auth_bp)
app.register_blueprint(recipes_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(pantry_bp)
app.register_blueprint(buddies_bp)

# ── DB teardown ───────────────────────────────────────────────────────────────
app.teardown_appcontext(close_db)


# ── Context processor ─────────────────────────────────────────────────────────
@app.context_processor
def inject_user():
    """Make the current logged-in user available in every Jinja2 template as 'current_user'."""
    return {"current_user": get_current_user()}


# ── Main routes ───────────────────────────────────────────────────────────────
@app.route("/")
def home():
    """Root route: redirect to the recipe list if logged in, otherwise to the login page."""
    user = get_current_user()
    if not user:
        return redirect(url_for("auth.login_page"))
    return redirect(url_for("recipes.list_recipes"))


# ── Error handlers ────────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    """Render a custom 404 page when a route or resource is not found."""
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    """Render a custom 500 page when an unhandled server-side exception occurs."""
    return render_template("500.html"), 500


# ── Startup ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_schema()
    print("Schema initialised.")
    restarted = recipe_images.recover_stale_pending()
    if restarted:
        print(f"Restarted {restarted} stale recipe image job(s).")
    debug = os.getenv("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(debug=debug, host="0.0.0.0", port=5001)
