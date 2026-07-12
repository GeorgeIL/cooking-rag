-- Cooking RAG — PostgreSQL schema
-- Run once at app startup via db.init_schema(); safe to re-run (IF NOT EXISTS).

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── Users ────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    username      VARCHAR(30) NOT NULL UNIQUE,
    email         VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Pantry (per-user ingredient list) ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pantry (
    user_id    UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ingredient VARCHAR(100) NOT NULL,
    PRIMARY KEY (user_id, ingredient)
);

-- ── Favorites (user ↔ recipe slug) ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS favorites (
    user_id     UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipe_slug VARCHAR(255) NOT NULL,
    PRIMARY KEY (user_id, recipe_slug)
);

-- ── Recipes (user-added recipes; 1100 CSV recipes live in Bedrock KB) ────────

CREATE TABLE IF NOT EXISTS recipes (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            VARCHAR(255) NOT NULL UNIQUE,
    title           VARCHAR(500) NOT NULL,
    description     TEXT         NOT NULL DEFAULT '',
    ingredients     JSONB        NOT NULL DEFAULT '[]',
    steps           JSONB        NOT NULL DEFAULT '[]',
    notes           TEXT         NOT NULL DEFAULT '',
    tags            JSONB        NOT NULL DEFAULT '[]',
    author_id       UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    author_username VARCHAR(30)  NOT NULL,
    s3_key          VARCHAR(1000) NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS recipes_author_idx     ON recipes(author_id);
CREATE INDEX IF NOT EXISTS recipes_created_at_idx ON recipes(created_at DESC);
CREATE INDEX IF NOT EXISTS pantry_user_idx        ON pantry(user_id);
CREATE INDEX IF NOT EXISTS favorites_user_idx     ON favorites(user_id);

-- ── Chat (one conversation per user, messages ordered by time) ────────────────

CREATE TABLE IF NOT EXISTS conversations (
    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS messages_conv_time_idx ON messages(conversation_id, created_at);
