/* ── Chat interface ──────────────────────────────────────────────────────────── */

const messagesEl = () => document.getElementById("messagesContainer");
const inputEl = () => document.getElementById("questionInput");
const sendBtnEl = () => document.getElementById("sendBtn");

function scrollToBottom(smooth = true) {
  const el = messagesEl();
  if (el)
    el.scrollTo({
      top: el.scrollHeight,
      behavior: smooth ? "smooth" : "instant",
    });
}

function autoResize(textarea) {
  textarea.style.height = "auto";
  textarea.style.height = Math.min(textarea.scrollHeight, 160) + "px";
}

function handleKeyDown(event) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
}

function fillQuestion(text) {
  const input = inputEl();
  if (!input) return;
  input.value = text;
  autoResize(input);
  input.focus();
}

/* ── Send message ────────────────────────────────────────────────────────────── */

async function sendMessage() {
  const input = inputEl();
  const question = (input.value || "").trim();
  if (!question) return;

  const sendBtn = sendBtnEl();
  const alertEl = document.getElementById("alert");
  alertEl.style.display = "none";

  // Append user bubble
  appendMessage("user", question);
  input.value = "";
  input.style.height = "auto";
  sendBtn.disabled = true;

  // Show thinking indicator
  const thinkingId = appendThinking();

  try {
    const resp = await fetch("/chat/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await resp.json();

    removeThinking(thinkingId);

    if (resp.ok) {
      appendMessage("assistant", data.answer);
    } else {
      showAlert(data.error || "Something went wrong", "error");
      appendMessage("assistant", `⚠️ ${data.error || "An error occurred."}`);
    }
  } catch {
    removeThinking(thinkingId);
    showAlert("Network error — please try again", "error");
  } finally {
    sendBtn.disabled = false;
    scrollToBottom();
  }
}

/* ── DOM helpers ─────────────────────────────────────────────────────────────── */

function appendMessage(role, content) {
  const container = messagesEl();
  const div = document.createElement("div");
  div.className = `message message--${role}`;

  const contentHtml =
    role === "assistant"
      ? typeof marked !== "undefined"
        ? marked.parse(content)
        : escapeHtml(content)
      : escapeHtml(content);

  div.innerHTML = `
    <div class="message-bubble">
      <div class="message-content">${contentHtml}</div>
    </div>
    <span class="message-role">${role === "user" ? "👤 You" : "👨‍🍳 Chef AI"}</span>
  `;
  container.appendChild(div);
  // Detect recipe-json blocks and attach "Add to Cookbook" buttons
  if (role === "assistant") attachRecipeButtons(div);
  scrollToBottom();
  return div;
}

function appendThinking() {
  const container = messagesEl();
  const div = document.createElement("div");
  div.className = "message message--assistant thinking-bubble";
  div.id = "thinking-" + Date.now();
  div.innerHTML = `<div class="message-bubble"><div class="message-content"></div></div>`;
  container.appendChild(div);
  scrollToBottom();
  return div.id;
}

function removeThinking(id) {
  const el = document.getElementById(id);
  if (el) el.remove();
}

/* ── AI recipe save ─────────────────────────────────────────────────────────── */

/**
 * Scan an assistant message div for recipe-json code blocks.
 * When found: hide the raw JSON block and inject an "Add to Cookbook" button.
 */
function attachRecipeButtons(div) {
  div.querySelectorAll("code.language-recipe-json").forEach((code) => {
    let data;
    try {
      data = JSON.parse(code.textContent);
    } catch {
      return; // Not valid JSON - skip silently
    }

    // Hide the raw JSON <pre> block so users don't see it
    const pre = code.closest("pre");
    if (pre) pre.style.display = "none";

    // Build the save button and place it below the message bubble
    const btn = document.createElement("button");
    btn.className = "btn-add-recipe";
    btn.innerHTML = "📥 Add to My Cookbook";
    btn.onclick = () => saveAiRecipe(data, btn);

    const bubble = div.querySelector(".message-bubble");
    if (bubble) bubble.after(btn);
  });
}

/**
 * POST the extracted recipe JSON to the backend and redirect to the new recipe page.
 */
async function saveAiRecipe(data, btn) {
  btn.disabled = true;
  btn.textContent = "⏳ Saving...";
  try {
    const resp = await fetch("/recipes/from-chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const result = await resp.json();
    if (resp.ok) {
      btn.textContent = "✅ Saved! Opening...";
      btn.style.background = "var(--success, #2d6a4f)";
      setTimeout(() => window.open(result.redirect, "_blank"), 700);
    } else {
      btn.disabled = false;
      btn.textContent = "📥 Add to My Cookbook";
      showAlert(result.error || "Failed to save recipe", "error");
    }
  } catch {
    btn.disabled = false;
    btn.textContent = "📥 Add to My Cookbook";
    showAlert("Network error - could not save recipe", "error");
  }
}

/* ── Index / history controls ────────────────────────────────────────────────── */

async function reloadIndex() {
  const resp = await fetch("/chat/reload-index", { method: "POST" });
  const data = await resp.json();
  alert(resp.ok ? data.message : data.error || "Failed to rebuild index");
  if (resp.ok) window.location.reload();
}

async function clearHistory() {
  if (!confirm("Clear all conversation history?")) return;
  const resp = await fetch("/chat/clear", { method: "POST" });
  if (resp.ok) window.location.reload();
}

/* ── Utilities ───────────────────────────────────────────────────────────────── */

function showAlert(msg, type = "error") {
  const el = document.getElementById("alert");
  el.textContent = msg;
  el.className = `alert alert--${type}`;
  el.style.display = "block";
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\n/g, "<br>");
}
