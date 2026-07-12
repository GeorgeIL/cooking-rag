# 🧑‍🍳 Smart Cookbook

**Your fridge, your questions, real recipes.** A RAG-powered kitchen assistant:
tell it what you've got, ask what to cook, and get answers grounded in your own
cookbook — not generic internet mush. Built on Flask + **AWS Bedrock (Nova
Lite)**, Aurora PostgreSQL, and a Bedrock Knowledge Base, with **one-command**
infrastructure setup and deploy.

> _"What can I make with chicken, rice and onions?"_ — the question my girlfriend
> and I kept typing into ChatGPT. So I built a cookbook that actually knows our
> recipes and answers it properly.

---

## What it does

- **Chat with Chef AI** — ask in plain English; answers are grounded in recipes
  retrieved from your Knowledge Base, and tailored to the ingredients in your
  pantry.
- **Manage your cookbook** — add, edit, upload (PDF/TXT), and favourite recipes.
  Uploads are parsed into structured recipes by the LLM.
- **Smart pantry** — track what you have; the assistant prioritises recipes you
  can actually make right now.
- **Auto-updating retrieval** — every recipe change re-syncs the Bedrock
  Knowledge Base, so search stays current.

---

## Architecture

```
┌─────────────┐   JWT cookie    ┌───────────────────────────────────────────┐
│   Browser   │ ◄─────────────► │   Flask 3 (app.py)                        │
│ (HTML/CSS/  │                 │   Blueprints: /auth /recipes /chat /pantry│
│    JS)      │                 └──────────────────┬────────────────────────┘
└─────────────┘                                    │
                    ┌──────────────────────────────┼──────────────────────────┐
       ┌────────────▼────────────┐    ┌────────────▼──────────┐  ┌────────────▼──────────┐
       │  Aurora PostgreSQL 17   │    │   RAG Engine          │  │  Amazon Bedrock       │
       │  (AWS RDS, IAM auth)    │    │   (rag/engine.py)     │  │  Nova Lite 1.0 (LLM)  │
       │  users, recipes,        │    │  retrieve_chunks() ───┼──┤  Titan Embeddings     │
       │  pantry, conversations  │    │  ask_chef()           │  └───────────────────────┘
       └─────────────────────────┘    │  sync_knowledge_base()│  ┌───────────────────────┐
                                       └───────────┬───────────┘  │  Amazon S3            │
                              Bedrock Knowledge Base│◄─────────────│  recipes/*.md         │
                              (OpenSearch Serverless vectors)      └───────────────────────┘

Deployment: Docker container on EC2 Ubuntu 24.04 — IAM instance role, no stored credentials.
```

| Layer         | Technology                                 | Role                                            |
| ------------- | ------------------------------------------ | ----------------------------------------------- |
| Web           | Flask 3 + Blueprints                       | Routing, auth, templates                        |
| Database      | Aurora PostgreSQL 17 (IAM token auth)      | Users, recipes, pantry, conversations           |
| Retrieval     | Amazon Bedrock Knowledge Base              | Semantic search over S3 recipe `.md` files      |
| LLM           | Amazon Nova Lite 1.0 (Bedrock Converse)    | Chat, recipe generation, upload parsing         |
| Storage       | Amazon S3                                  | Recipe markdown + KB data source                |
| Frontend      | Jinja2 + vanilla JS + marked.js            | Chat UI, recipe CRUD, pantry, dark mode         |
| Deploy        | Docker on EC2 Ubuntu 24.04 (t3.micro)      | IAM instance role provides all AWS credentials  |

---

## 🚀 Setup — from zero to deployed

Everything below is scripted. You fill in a handful of values; the scripts create
every AWS resource and wire the IDs back into your config.

**Prerequisites:** an AWS account, the [AWS CLI](https://docs.aws.amazon.com/cli/)
configured, Docker, an EC2 key pair, and Bedrock **model access enabled** for
*Nova Lite* and *Titan Text Embeddings V2* (Bedrock console → Model access).

```bash
# 1. Configure — copy the template and fill in the <...> values
cp .env.example .env      # AWS keys, a bucket name, a DB password, KEY_NAME

# 2. Provision the whole AWS stack (S3, IAM, Aurora, Bedrock KB) — idempotent
./setup_aws.sh            # writes the generated KB / DB IDs back into .env

# 3. Deploy to EC2 (builds the image on the instance, runs the container)
./deploy.sh               # prints the live URL when the app responds

# When you're done, stop paying for it:
./teardown_aws.sh         # add --all to also remove the bucket + IAM roles
```

> 💸 **Cost note:** `setup_aws.sh` creates an Aurora cluster and an **OpenSearch
> Serverless** collection (the KB vector store), which has a high minimum monthly
> cost. Already have a Knowledge Base? Skip that charge:
> `./setup_aws.sh --reuse-kb <KB_ID> <DS_ID>`.

### Run locally (no deploy)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py             # http://localhost:5001  (uses AWS creds from .env)
```

Or with Docker: `docker build -t cooking-rag . && docker run -p 5001:5001 --env-file .env cooking-rag`

---

## Project structure

```
cooking-rag/
├── app.py                 # Flask app factory, blueprints, error handlers
├── config.py              # Config loaded from .env
├── db.py                  # Aurora connection pool + IAM token cache
├── auth_utils.py          # JWT helpers
├── rag/engine.py          # retrieve_chunks(), ask_chef(), sync_knowledge_base()
├── routes/                # auth, chat, recipes, pantry blueprints
├── migrations/schema.sql  # CREATE TABLE IF NOT EXISTS — applied on startup
├── data/recipes/          # Seed recipes uploaded to S3 by setup_aws.sh
├── static/ · templates/   # CSS/JS · Jinja2 HTML
├── Dockerfile
├── setup_aws.sh           # ← provisions the entire AWS stack from .env
├── deploy.sh              # ← builds + runs the container on a fresh EC2
└── teardown_aws.sh        # ← removes the billable resources
```

---

## How a chat request flows

1. User asks a question → `POST /chat/ask`.
2. `retrieve_chunks()` queries the Bedrock Knowledge Base for relevant recipe
   passages.
3. `ask_chef()` calls Nova Lite via the Converse API with the system prompt,
   recent history, retrieved chunks, and the user's pantry.
4. If the model invents a brand-new recipe it appends a hidden `recipe-json`
   block; the frontend turns it into an **"Add to My Cookbook"** button.
5. Both messages are stored in Aurora with `clock_timestamp()` to preserve order.

---

## Notes from the AWS build

- **IAM everywhere, secrets nowhere** — the EC2 instance role supplies AWS
  credentials via the metadata service, and Aurora uses short-lived IAM tokens
  (cached 14 min) instead of passwords.
- **Cluster vs. instance endpoint** — Aurora IAM token auth only validates
  against the **cluster writer endpoint**; the instance endpoint returns an
  opaque "PAM authentication failed". `setup_aws.sh` always writes the cluster
  endpoint.
- **`rds-db:connect` is separate** — `AmazonRDSFullAccess` covers the management
  plane but not database login; the scripts add that inline policy explicitly.

See `Pictures/` for screenshots of the running app, the Bedrock Knowledge Base,
and the EC2 deployment.
