/* ── Cooking buddies CRUD ──────────────────────────────────────────────────── */

const DEFAULT_AVATAR = "/static/img/buddy-default.svg";
let editingBuddyId = null;
let selectedRecipe = null;

function buddyAvatarUrl(pictureUrl) {
  const url = (pictureUrl || "").trim();
  return url || DEFAULT_AVATAR;
}

function openBuddyModal(buddy = null) {
  editingBuddyId = buddy ? buddy.id : null;
  document.getElementById("buddyModalTitle").textContent = buddy
    ? "Edit Buddy"
    : "Add Buddy";
  document.getElementById("buddyNameInput").value = buddy ? buddy.name : "";
  document.getElementById("buddyEmailInput").value = buddy ? buddy.email : "";
  document.getElementById("buddyPictureInput").value = buddy
    ? buddy.picture_url || ""
    : "";
  updateBuddyPreview();
  document.getElementById("buddyModal").classList.add("open");
}

function closeBuddyModal() {
  editingBuddyId = null;
  document.getElementById("buddyModal").classList.remove("open");
}

function updateBuddyPreview() {
  const url = document.getElementById("buddyPictureInput").value.trim();
  document.getElementById("buddyPreviewImg").src = buddyAvatarUrl(url);
}

async function saveBuddy() {
  const name = document.getElementById("buddyNameInput").value.trim();
  const email = document.getElementById("buddyEmailInput").value.trim();
  const picture_url = document.getElementById("buddyPictureInput").value.trim();
  const alertEl = document.getElementById("alert");
  alertEl.style.display = "none";

  const payload = { name, email, picture_url };
  const url = editingBuddyId ? `/buddies/${editingBuddyId}` : "/buddies/";
  const method = editingBuddyId ? "PUT" : "POST";

  try {
    const resp = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (resp.ok) {
      closeBuddyModal();
      window.location.reload();
      return;
    }
    showAlert(data.error || "Failed to save buddy", "error");
  } catch {
    showAlert("Network error", "error");
  }
}

async function deleteBuddy(buddyId, btn) {
  if (!confirm("Remove this cooking buddy?")) return;
  const card = btn.closest(".buddy-card");
  try {
    const resp = await fetch(`/buddies/${buddyId}`, { method: "DELETE" });
    if (resp.ok) {
      card.remove();
      checkBuddiesEmpty();
      updateBuddyCount();
    } else {
      const data = await resp.json();
      showAlert(data.error || "Failed to delete buddy", "error");
    }
  } catch {
    showAlert("Network error", "error");
  }
}

function checkBuddiesEmpty() {
  const empty = document.getElementById("emptyState");
  if (!empty) return;
  const hasCards = document.querySelectorAll(".buddy-card").length > 0;
  empty.style.display = hasCards ? "none" : "block";
}

function updateBuddyCount() {
  const counter = document.getElementById("buddyCount");
  if (!counter) return;
  const n = document.querySelectorAll(".buddy-card").length;
  counter.textContent = `${n} buddy${n !== 1 ? "ies" : ""}`;
}

function showAlert(msg, type = "error") {
  const el = document.getElementById("alert");
  if (!el) return;
  el.textContent = msg;
  el.className = `alert alert--${type}`;
  el.style.display = "block";
  setTimeout(() => {
    el.style.display = "none";
  }, 4000);
}

/* ── Share a recipe (buddies page) ───────────────────────────────────────── */

function updateSelectedRecipeDisplay() {
  const el = document.getElementById("selectedRecipeDisplay");
  if (!el) return;
  if (!selectedRecipe) {
    el.innerHTML = '<span class="sidebar-hint">No recipe selected</span>';
    return;
  }
  const badgeClass =
    selectedRecipe.source === "user"
      ? "recipe-source-badge recipe-source-badge--user"
      : "recipe-source-badge";
  const badgeLabel = selectedRecipe.source === "user" ? "Yours" : "Catalog";
  el.innerHTML = `
    <span class="recipe-selected-chip">
      <span>${escapeHtml(selectedRecipe.title)}</span>
      <span class="${badgeClass}">${badgeLabel}</span>
    </span>`;
}

function setSelectedRecipe(recipe) {
  selectedRecipe = recipe;
  updateSelectedRecipeDisplay();
  const subjectInput = document.getElementById("shareSubjectInput");
  if (subjectInput) {
    subjectInput.value = `Recipe: ${recipe.title}`;
  }
}

async function sendRecipeEmail() {
  if (!selectedRecipe) {
    showAlert("Select a recipe first", "error");
    return;
  }

  const checked = Array.from(
    document.querySelectorAll('input[name="shareBuddyPick"]:checked'),
  ).map((el) => el.value);

  if (!checked.length) {
    showAlert("Select at least one buddy", "error");
    return;
  }

  const subject = document.getElementById("shareSubjectInput").value.trim();
  const personal_note = document.getElementById("shareNoteInput").value.trim();

  try {
    const resp = await fetch("/buddies/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        buddy_ids: checked,
        s3_key: selectedRecipe.s3_key,
        subject,
        personal_note,
      }),
    });
    const data = await resp.json();
    if (resp.ok) {
      showAlert(data.message, "success");
      document.getElementById("shareNoteInput").value = "";
      document.querySelectorAll('input[name="shareBuddyPick"]:checked').forEach((el) => {
        el.checked = false;
      });
      return;
    }
    showAlert(data.error || "Failed to send email", "error");
  } catch {
    showAlert("Network error", "error");
  }
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

document.addEventListener("DOMContentLoaded", () => {
  const pictureInput = document.getElementById("buddyPictureInput");
  if (pictureInput) {
    pictureInput.addEventListener("input", updateBuddyPreview);
  }

  const params = new URLSearchParams(window.location.search);
  const s3Key = params.get("s3_key");
  const title = params.get("title");
  const source = params.get("source") || "catalog";

  if (s3Key && title) {
    setSelectedRecipe({ s3_key: s3Key, title, source });
    if (window.history.replaceState) {
      window.history.replaceState({}, "", window.location.pathname);
    }
  }
});
