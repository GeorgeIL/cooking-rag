/* ── Pantry management ──────────────────────────────────────────────────────── */

async function addIngredient() {
  const input = document.getElementById("ingredientInput");
  const ingredient = (input.value || "").trim().toLowerCase();
  const alertEl = document.getElementById("alert");
  alertEl.style.display = "none";

  if (!ingredient) {
    showAlert("Please enter an ingredient name", "error");
    return;
  }

  // Check duplicate in current list
  const existing = Array.from(
    document.querySelectorAll(".pantry-item-name"),
  ).map((el) => el.textContent.trim().toLowerCase());
  if (existing.includes(ingredient)) {
    showAlert(`"${ingredient}" is already in your pantry`, "info");
    input.value = "";
    return;
  }

  try {
    const resp = await fetch("/pantry/ingredients", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ingredient }),
    });
    const data = await resp.json();

    if (resp.ok) {
      appendItem(ingredient);
      input.value = "";
      input.focus();
    } else {
      showAlert(data.error || "Failed to add ingredient", "error");
    }
  } catch {
    showAlert("Network error", "error");
  }
}

async function removeIngredient(ingredient, btn) {
  const row = btn.closest(".pantry-item");
  try {
    const resp = await fetch(
      `/pantry/ingredients/${encodeURIComponent(ingredient)}`,
      {
        method: "DELETE",
      },
    );
    if (resp.ok) {
      row.remove();
      updateCount();
      checkEmpty();
    }
  } catch {
    // silent fail on remove
  }
}

async function clearPantry() {
  if (!confirm("Remove all ingredients from your pantry?")) return;
  try {
    const resp = await fetch("/pantry/clear", { method: "POST" });
    if (resp.ok) {
      document.querySelectorAll(".pantry-item").forEach((el) => el.remove());
      updateCount();
      checkEmpty();
    }
  } catch {
    // silent
  }
}

/* ── DOM helpers ────────────────────────────────────────────────────────────── */

function appendItem(ingredient) {
  const list = document.getElementById("pantryList");

  // Hide empty state
  const empty = document.getElementById("emptyState");
  if (empty) empty.style.display = "none";

  const item = document.createElement("div");
  item.className = "pantry-item";
  item.innerHTML = `
    <span class="pantry-item-name">${escapeHtml(ingredient)}</span>
    <button class="btn-remove-icon"
            onclick="removeIngredient('${escapeAttr(ingredient)}', this)"
            title="Remove">✕</button>
  `;
  list.appendChild(item);
  updateCount();
}

function updateCount() {
  const counter = document.getElementById("pantryCount");
  if (!counter) return;
  const n = document.querySelectorAll(".pantry-item").length;
  counter.textContent = `${n} ingredient${n !== 1 ? "s" : ""}`;
}

function checkEmpty() {
  const empty = document.getElementById("emptyState");
  if (!empty) return;
  const hasItems = document.querySelectorAll(".pantry-item").length > 0;
  empty.style.display = hasItems ? "none" : "block";
}

function showAlert(msg, type = "error") {
  const el = document.getElementById("alert");
  el.textContent = msg;
  el.className = `alert alert--${type}`;
  el.style.display = "block";
  setTimeout(() => {
    el.style.display = "none";
  }, 4000);
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapeAttr(str) {
  return String(str).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
