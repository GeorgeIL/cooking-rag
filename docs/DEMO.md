# 🍳 Smart Cookbook — 5-Step Demo

A quick walkthrough that shows off every core capability. Run it against your
deployed site (e.g. `http://<your-ec2-ip>:5001`).

### 1. Sign up & log in
Create an account and log in.
→ *Shows: JWT auth backed by Aurora PostgreSQL with passwordless IAM-token DB access.*

### 2. Ask about a saved recipe
In chat, ask: *"From the cookbook, what's in the Very Best Chicken Noodle Soup?"*
→ *Shows: RAG — the Bedrock Agent retrieves the real recipe from the Knowledge Base, no hallucination.*

### 3. Create a recipe & watch the photo appear
Add a new recipe (any dish), then open it and wait a few seconds.
→ *Shows: recipe saved to S3 + Aurora, and Google Gemini auto-generates a food photo (stored in S3).*

### 4. Ask what to cook from your pantry
Add a few pantry items (e.g. eggs, spinach, cheddar), then ask in chat: *"What can I make with my pantry?"*
→ *Shows: the agent tailors suggestions to what you actually have — reliably.*

### 5. Invent a new recipe in chat & save it
Ask: *"Invent a new original recipe for spicy mango black bean tacos."* then click **Add to My Cookbook**.
→ *Shows: the agent generates an original recipe and one click saves it (which also triggers its own AI photo).*
