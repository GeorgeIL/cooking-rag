import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")

    # JWT
    JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "24"))

    # AWS
    AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

    # Bedrock Knowledge Base
    BEDROCK_KB_ID = os.getenv("BEDROCK_KB_ID")
    BEDROCK_KB_DS_ID = os.getenv("BEDROCK_KB_DS_ID")
    NOVA_MODEL_ID = os.getenv("NOVA_MODEL_ID", "amazon.nova-lite-v1:0")

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
    HISTORY_MESSAGES = int(os.getenv("HISTORY_MESSAGES", "6"))

    # Data folder
    DATA_FOLDER = BASE_DIR / "data"
