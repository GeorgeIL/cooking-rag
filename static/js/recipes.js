/* ── Recipe form: dynamic ingredient / step rows ─────────────────────────── */

function addIngredient(value = "") {
  const list = document.getElementById("ingredients-list");
  const row = document.createElement("div");
  row.className = "dynamic-row";
  row.innerHTML = `
    <span class="row-icon">·</span>
    <input type="text" class="ingredient-input"
           placeholder="e.g. 200g spaghetti"
           value="${escapeAttr(value)}" />
    <button type="button" class="btn-remove" onclick="removeRow(this)" title="Remove">✕</button>
  `;
  list.appendChild(row);
  row.querySelector("input").focus();
}

function addStep(value = "") {
  const list = document.getElementById("steps-list");
  const index = list.querySelectorAll(".dynamic-row").length + 1;
  const row = document.createElement("div");
  row.className = "dynamic-row";
  row.innerHTML = `
    <span class="step-number">${index}</span>
    <textarea class="step-input" rows="2"
              placeholder="Describe this step…">${escapeHtml(value)}</textarea>
    <button type="button" class="btn-remove" onclick="removeStep(this)" title="Remove">✕</button>
  `;
  list.appendChild(row);
  row.querySelector("textarea").focus();
}

function removeRow(btn) {
  btn.closest(".dynamic-row").remove();
}

function removeStep(btn) {
  btn.closest(".dynamic-row").remove();
  renumberSteps();
}

function renumberSteps() {
  document.querySelectorAll("#steps-list .step-number").forEach((el, i) => {
    el.textContent = i + 1;
  });
}

/* ── Save / update recipe ───────────────────────────────────────────────────── */

async function saveRecipe(action, slug) {
  const alertEl = document.getElementById("alert");
  alertEl.style.display = "none";

  const title = (document.getElementById("title").value || "").trim();
  const description = (
    document.getElementById("description").value || ""
  ).trim();
  const notes = (document.getElementById("notes").value || "").trim();
  const tagsRaw = document.getElementById("tags").value || "";
  const tags = tagsRaw
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);

  const ingredients = Array.from(document.querySelectorAll(".ingredient-input"))
    .map((el) => el.value.trim())
    .filter(Boolean);

  const steps = Array.from(document.querySelectorAll(".step-input"))
    .map((el) => el.value.trim())
    .filter(Boolean);

  if (!title) {
    showAlert(alertEl, "Recipe title is required", "error");
    return;
  }
  if (ingredients.length === 0) {
    showAlert(alertEl, "Add at least one ingredient", "error");
    return;
  }
  if (steps.length === 0) {
    showAlert(alertEl, "Add at least one step", "error");
    return;
  }

  const method = action === "create" ? "POST" : "PUT";
  const url = action === "create" ? "/recipes/" : `/recipes/${slug}`;
  const btn = document.getElementById("submitBtn");
  btn.disabled = true;

  try {
    const resp = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title,
        description,
        ingredients,
        steps,
        notes,
        tags,
      }),
    });
    const data = await resp.json();
    if (resp.ok) {
      window.location.href = data.redirect;
    } else {
      showAlert(alertEl, data.error || "Save failed", "error");
    }
  } catch {
    showAlert(alertEl, "Network error — please try again", "error");
  } finally {
    btn.disabled = false;
  }
}

/* ── Delete recipe ──────────────────────────────────────────────────────────── */

async function deleteRecipe(slug) {
  if (!confirm("Delete this recipe? This cannot be undone.")) return;

  try {
    const resp = await fetch(`/recipes/${slug}`, { method: "DELETE" });
    const data = await resp.json();
    if (resp.ok) {
      window.location.href = data.redirect;
    } else {
      alert(data.error || "Delete failed");
    }
  } catch {
    alert("Network error");
  }
}

/* ── Utilities ──────────────────────────────────────────────────────────────── */

function showAlert(el, msg, type = "error") {
  el.textContent = msg;
  el.className = `alert alert--${type}`;
  el.style.display = "block";
  el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function escapeAttr(str) {
  return String(str).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
