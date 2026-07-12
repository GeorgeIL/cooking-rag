# Smart Cookbook — Architecture & How It Works (End to End)

A Flask web app that turns "what can I cook?" into grounded answers and full
recipes, backed by AWS + Google Gemini.

## 1. The 10,000-foot view

```
Browser ──HTTP──> Flask (EC2, Docker) ──> Aurora PostgreSQL     (users, recipes, pantry, chats)
                                    ├────> Bedrock Agent         (Chef AI chat + tools)
                                    ├────> Bedrock Knowledge Base ──> S3 recipe .md files (RAG)
                                    ├────> Bedrock Nova Lite      (recipe parsing/generation)
                                    ├────> Google Gemini          (recipe photos) ──> S3
                                    └────> SES + Lambda           (email recipes to "buddies")
```

## 2. Request lifecycle — a chat message from click to answer

`POST /chat/ask` (`routes/chat.py`):

1. **Auth** — `login_required` verifies the `token` JWT cookie (`auth_utils.py`); no valid token → 401.
2. **Gather context from Aurora** — pantry ingredients, recent conversation history, cooking buddies, any active recipe.
3. **Build the agent call** — pack context into `sessionAttributes` (durable: `user_id`, buddy contacts) and `promptSessionAttributes` (per-turn: `pantry`, `buddy_names`, `active_recipe`).
4. **Invoke the Bedrock Agent** (`services/bedrock_agent.py`) with the question + attributes + a per-user `sessionId`. The agent chooses to search the KB, call an action-group tool, or answer directly.
5. **Assemble** the streamed response; a hidden `recipe-json` block (if present) becomes an "Add to cookbook" button.
6. **Persist** both messages to Aurora (`clock_timestamp()` preserves order); return JSON.

## 3. The components

- **Flask (`app.py`)** — app factory; 5 blueprints (`auth`, `recipes`, `chat`, `pantry`, `buddies`). On boot: `init_schema()` (applies `migrations/schema.sql`, idempotent) and `recipe_images.recover_stale_pending()`. Serves on **:5001**.
- **Aurora PostgreSQL + IAM auth (`db.py`)** — no DB password stored. `boto3.generate_db_auth_token()` mints a 15-min IAM token (cached 14 min), used as the password over SSL through a `ThreadedConnectionPool`. The `postgres` user was granted `rds_iam` once.
- **RAG (`rag/engine.py`)** — `retrieve_chunks()` (Bedrock KB semantic search over S3 `.md`), `ask_chef()` (Nova Lite Converse), `sync_knowledge_base()` (fires an ingestion job on recipe change; ~30 s–minutes lag).
- **Chef AI Agent (`services/bedrock_agent.py`)** — a Bedrock Agent (`BEDROCK_AGENT_ID`, alias `BEDROCK_AGENT_ALIAS_ID`) orchestrates KB + action-group tools with per-user sessions (`conversations` table).
- **Recipe photos (`services/recipe_images.py`)** — async: on create, write a `pending` row (`recipe_images` table) + spawn a background thread → call Gemini (`gemini-3.1-flash-image`) → upload JPEG to `recipes/catalog/images/<slug>.jpg` → flip row to `ready`. UI polls `/recipes/<slug>/image/status`. Users can also upload/paste a URL.
- **S3** — recipe markdown (`recipes/`), generated images (`recipes/catalog/images/`), and the KB data source.
- **Cooking buddies** — share a recipe by email via an SES Lambda (`lambda/buddy_email/`), callable by the agent.

## 4. The AWS stack & how the parts connect

```
                        EC2 instance (Ubuntu, Docker)
                        │  IAM instance role: cooking-rag-ec2-role
                        │  (AmazonBedrockFullAccess, S3FullAccess,
                        │   RDSFullAccess, + rds-db:connect inline)
   Internet ──:5001──►  │  container: cooking-rag  (Flask :5001)
   (SG: 22,5001)        │      │
                        │      ├─ boto3 uses the INSTANCE ROLE (no keys on disk)
                        │      │     ├─► Bedrock Agent / KB / Nova
                        │      │     ├─► S3
                        │      │     └─► RDS: generate IAM token ──► Aurora (SSL)
                        │      └─ Gemini key from env file ──► Google Gemini API
```

- **Credentials:** the EC2 instance role supplies AWS credentials via the metadata service — no AWS secret on the box. The only real secret shipped to the server is the **Gemini key** (Google has no instance-role equivalent).
- **Aurora** is internet-reachable via RDS's Internet-Access-Gateway relay (so 5432 is open) but protected by **IAM token auth**, not a password.
- **Network:** security group `cooking-rag-sg` opens only 22 (SSH) and 5001 (app).

## 5. How `.env` reaches the app — three paths

Config is centralized in `config.py` (`load_dotenv(BASE_DIR/.env)` → `Config.*`). How that `.env` gets populated differs:

**A) Local dev** — you edit `./.env`; it includes your IAM user's AWS keys (no instance role locally).

**B) Docker locally** — `docker run --env-file .env`. The Dockerfile does **not** bake `.env`; config is always injected at runtime, so the image is safe to push and carries no secrets.

**C) EC2 (production) via `deploy.sh`:**
```
your ./.env ──deploy.sh reads it──► RUNTIME_ENV
                                     (drops AWS_ACCESS_KEY_ID/SECRET — instance role provides those)
                                     (keeps GEMINI_API_KEY, KB/agent/S3/RDS values)
   └► embedded in EC2 cloud-init user-data ──► /opt/app/.env on the box
      └► docker run --env-file /opt/app/.env ──► container env ──► config.py
```
`RDS_HOST` is rewritten to the **cluster** endpoint (IAM token auth only validates there).

## 6. The deployment scripts

- **`setup_aws.sh`** (idempotent, one-time) — from `.env`: S3 bucket + seed recipes, IAM role/instance-profile (incl. `rds-db:connect`), security group, Aurora cluster (waits ~10 min), `rds_iam` grant + schema, and the full Bedrock Knowledge Base (OpenSearch Serverless collection + policies + SigV4-signed vector index + KB + data source + first ingestion). Writes generated IDs back to `.env`. `--reuse-kb` skips the pricey OpenSearch part.
- **`deploy.sh`** — launches a fresh Ubuntu EC2; cloud-init installs Docker, clones the public repo, builds natively (amd64, no registry), writes `/opt/app/.env`, runs the container, polls until healthy, prints the URL.
- **`teardown_aws.sh`** — removes the billable resources (`--all` also drops the bucket + IAM roles).

## 7. Security model in one breath

JWT in an httpOnly cookie for users · IAM tokens (not passwords) for Aurora · instance role (not stored keys) for all AWS calls · config/secrets injected at runtime, never baked into the image · the sole server-side secret is the Gemini key · `.env` is gitignored so none of it reaches GitHub.

## 8. Chat agent internals & tools

The Chef AI agent runs on Amazon **Nova Pro** with one Lambda action group
(`lmbda.py`) exposing: `GetTime`, `GetWeather`, `SuggestDishForTimeAndWeather`,
and `ShareRecipeWithBuddy`. It also queries the Knowledge Base directly.
`promptSessionAttributes.pantry` carries the user's pantry into every turn, so
"what can I make?" is answered from the pantry + KB. Sessions are per-user and
can be rotated (`_reset_agent_session`) to clear a bad state.
