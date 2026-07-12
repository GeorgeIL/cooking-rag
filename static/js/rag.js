/* ── Helpers ─────────────────────────────────────────────────────────────── */

function setChunkInfo(n) {
  const el = document.getElementById("chunkInfo");
  if (el)
    el.textContent = `${n} text chunk${n !== 1 ? "s" : ""} indexed · embeddings via Hugging Face · answers via Gemini`;
}

function toggleContext(id) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle("hidden");
}

/* ── Append a message bubble ─────────────────────────────────────────────── */
let msgCounter = 0;

function appendMessage(role, text, contextChunks) {
  const messages = document.getElementById("messages");

  // Remove empty state placeholder on first message
  const es = document.getElementById("emptyState");
  if (es) es.remove();

  const id = `msg-${msgCounter++}`;
  const wrapper = document.createElement("div");
  wrapper.className = `msg ${role}`;

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrapper.appendChild(bubble);

  if (contextChunks && contextChunks.length > 0) {
    const ctxId = `ctx-${id}`;

    const toggleBtn = document.createElement("button");
    toggleBtn.className = "context-toggle";
    toggleBtn.textContent = "View retrieved context";
    toggleBtn.onclick = () => toggleContext(ctxId);
    wrapper.appendChild(toggleBtn);

    const cards = document.createElement("div");
    cards.className = "context-cards hidden";
    cards.id = ctxId;

    contextChunks.forEach((chunk, i) => {
      const card = document.createElement("div");
      card.className = "context-card";
      card.textContent = `#${i + 1} — ${chunk}`;
      cards.appendChild(card);
    });
    wrapper.appendChild(cards);
  }

  messages.appendChild(wrapper);
  messages.scrollTop = messages.scrollHeight;
}

/* ── Typing indicator ────────────────────────────────────────────────────── */
function showTyping() {
  const messages = document.getElementById("messages");
  const wrapper = document.createElement("div");
  wrapper.className = "msg assistant";
  wrapper.id = "typing-indicator";
  wrapper.innerHTML = `<div class="bubble typing"><span></span><span></span><span></span></div>`;
  messages.appendChild(wrapper);
  messages.scrollTop = messages.scrollHeight;
}

function hideTyping() {
  const el = document.getElementById("typing-indicator");
  if (el) el.remove();
}

/* ── Send a chat message ─────────────────────────────────────────────────── */
async function sendMessage() {
  const input = document.getElementById("chatInput");
  const sendBtn = document.getElementById("sendBtn");
  const question = input.value.trim();
  if (!question) return;

  input.value = "";
  input.disabled = true;
  sendBtn.disabled = true;

  appendMessage("user", question);
  showTyping();

  try {
    const res = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();
    hideTyping();

    if (data.error) {
      appendMessage("assistant", `⚠ Error: ${data.error}`);
    } else {
      appendMessage("assistant", data.answer, data.context);
    }
  } catch (err) {
    hideTyping();
    appendMessage("assistant", `⚠ Network error: ${err.message}`);
  } finally {
    input.disabled = false;
    sendBtn.disabled = false;
    input.focus();
  }
}

/* ── Upload a file ───────────────────────────────────────────────────────── */
async function uploadFile(input) {
  const status = document.getElementById("uploadStatus");
  const file = input.files[0];
  if (!file) return;

  status.textContent = `Uploading ${file.name}…`;
  status.className = "upload-status";

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch("/upload", { method: "POST", body: formData });
    const data = await res.json();

    if (data.error) {
      status.textContent = `✗ ${data.error}`;
      status.className = "upload-status error";
    } else {
      status.textContent = `✓ ${data.filename} added (${data.chunk_count} chunks)`;
      status.className = "upload-status success";
      setChunkInfo(data.chunk_count);
    }
  } catch (err) {
    status.textContent = `✗ Network error: ${err.message}`;
    status.className = "upload-status error";
  }

  // Reset so the same file can be re-uploaded if needed
  input.value = "";
}

/* ── Reload documents ────────────────────────────────────────────────────── */
async function reloadDocs() {
  const btn = document.getElementById("reloadBtn");
  const status = document.getElementById("uploadStatus");

  btn.disabled = true;
  btn.textContent = "⏳ Reloading…";

  try {
    const res = await fetch("/reload", { method: "POST" });
    const data = await res.json();

    if (data.error) {
      status.textContent = `✗ ${data.error}`;
      status.className = "upload-status error";
    } else {
      setChunkInfo(data.chunk_count);
      status.textContent = `✓ Index rebuilt (${data.chunk_count} chunks)`;
      status.className = "upload-status success";
    }
  } catch (err) {
    status.textContent = `✗ Network error: ${err.message}`;
    status.className = "upload-status error";
  } finally {
    btn.disabled = false;
    btn.textContent = "🔄\u00a0 Reload documents";
  }
}
