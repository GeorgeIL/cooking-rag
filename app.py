from flask import Flask, redirect, render_template, url_for

from auth_utils import get_current_user
from config import Config
from db import close_db, init_schema
from routes.auth import auth_bp
from routes.chat import chat_bp
from routes.pantry import pantry_bp
from routes.recipes import recipes_bp

app = Flask(__name__)
app.config.from_object(Config)

# ── Blueprints ────────────────────────────────────────────────────────────────
app.register_blueprint(auth_bp)
app.register_blueprint(recipes_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(pantry_bp)

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
    app.run(debug=True, host="0.0.0.0", port=5001)
