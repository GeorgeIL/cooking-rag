import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Load project .env explicitly (find_dotenv() can miss it depending on cwd).
load_dotenv(BASE_DIR / ".env")


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")

    # JWT
    JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "24"))

    # AWS
    AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

    # Bedrock Knowledge Base
    BEDROCK_KB_ID = os.getenv("BEDROCK_KB_ID")
    BEDROCK_KB_DS_ID = os.getenv("BEDROCK_KB_DS_ID")
    BEDROCK_KB_SYNC_ALL = os.getenv("BEDROCK_KB_SYNC_ALL", "false").lower() in (
        "1",
        "true",
        "yes",
    )
    NOVA_MODEL_ID = os.getenv("NOVA_MODEL_ID", "amazon.nova-lite-v1:0")
    NOVA_MAX_OUTPUT_TOKENS = int(os.getenv("NOVA_MAX_OUTPUT_TOKENS", "4096"))

    # Bedrock Agent (Chef AI chat)
    BEDROCK_AGENT_ID = os.getenv("BEDROCK_AGENT_ID", "")
    BEDROCK_AGENT_ALIAS_ID = os.getenv("BEDROCK_AGENT_ALIAS_ID", "")
    # If the production alias points at an old prepared version, retry TSTALIASID (DRAFT).
    BEDROCK_AGENT_FALLBACK_TO_DRAFT = os.getenv(
        "BEDROCK_AGENT_FALLBACK_TO_DRAFT", "true"
    ).lower() in ("1", "true", "yes")
    BEDROCK_AGENT_MAX_OUTPUT_TOKENS = int(
        os.getenv("BEDROCK_AGENT_MAX_OUTPUT_TOKENS", "8192")
    )
    AGENT_TOOL_SECRET = os.getenv("AGENT_TOOL_SECRET", "")
    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000")
    METEOSOURCE_API_KEY = os.getenv("METEOSOURCE_API_KEY", "")

    # S3
    S3_BUCKET = os.getenv("S3_BUCKET_NAME")
    S3_RECIPES_PREFIX = os.getenv("S3_RECIPES_PREFIX", "recipes/")

    # RDS PostgreSQL
    RDS_HOST = os.getenv("RDS_HOST", "localhost")
    RDS_PORT = int(os.getenv("RDS_PORT", "5432"))
    RDS_DB = os.getenv("RDS_DB", "cooking_rag")
    RDS_USER = os.getenv("RDS_USER", "postgres")
    RDS_PASSWORD = os.getenv("RDS_PASSWORD", "")

    # RAG
    TOP_K = int(os.getenv("TOP_K", "5"))
    HISTORY_MESSAGES = int(os.getenv("HISTORY_MESSAGES", "12"))
    HISTORY_MESSAGE_MAX_CHARS = int(os.getenv("HISTORY_MESSAGE_MAX_CHARS", "8000"))

    # Data folder
    DATA_FOLDER = BASE_DIR / "data"

    # Cooking buddies email (Lambda + SES; SES_FROM_EMAIL is used by Lambda, not Flask)
    BUDDY_EMAIL_LAMBDA_NAME = os.getenv(
        "BUDDY_EMAIL_LAMBDA_NAME", "cooking-rag-buddy-email"
    )
    SES_FROM_EMAIL = os.getenv("SES_FROM_EMAIL", "")

    # Gemini image generation
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-image")
