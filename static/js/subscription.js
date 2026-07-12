/* ── Fake Pro subscription modal (upload paywall) ─────────────────────────── */

const UPLOAD_PRO_BYPASS_KEY = "uploadProBypass";

function isUploadProBypassed() {
  return sessionStorage.getItem(UPLOAD_PRO_BYPASS_KEY) === "1";
}

function grantUploadProBypass() {
  sessionStorage.setItem(UPLOAD_PRO_BYPASS_KEY, "1");
}

function openSubscriptionModal() {
  const modal = document.getElementById("subscriptionModal");
  if (!modal) return;
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("subscription-modal-open");
  const toast = document.getElementById("subscriptionToast");
  if (toast) toast.hidden = true;
}

function closeSubscriptionModal() {
  const modal = document.getElementById("subscriptionModal");
  if (!modal) return;
  modal.classList.remove("open");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("subscription-modal-open");
}

function showSubscriptionToast() {
  const toast = document.getElementById("subscriptionToast");
  if (toast) {
    toast.hidden = false;
    setTimeout(() => {
      toast.hidden = true;
    }, 2800);
  }
}

function handleMaybeLater() {
  grantUploadProBypass();
  closeSubscriptionModal();

  const laterBtn = document.getElementById("subscriptionMaybeLater");
  const uploadUrl = laterBtn?.dataset.uploadUrl || "/recipes/upload";

  if (window.location.pathname.replace(/\/+$/, "").endsWith("/upload")) {
    initUploadPageNormal();
    return;
  }

  window.location.href = uploadUrl;
}

function initSubscriptionModal() {
  const modal = document.getElementById("subscriptionModal");
  if (!modal) return;

  const closeBtn = document.getElementById("subscriptionModalClose");
  const laterBtn = document.getElementById("subscriptionMaybeLater");

  closeBtn?.addEventListener("click", closeSubscriptionModal);
  laterBtn?.addEventListener("click", handleMaybeLater);

  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeSubscriptionModal();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && modal.classList.contains("open")) {
      closeSubscriptionModal();
    }
  });

  modal.querySelectorAll(".subscription-cta").forEach((btn) => {
    btn.addEventListener("click", showSubscriptionToast);
  });
}

function wireUploadRecipeButton(buttonId) {
  const btn = document.getElementById(buttonId);
  if (!btn) return;
  btn.addEventListener("click", (event) => {
    event.preventDefault();
    openSubscriptionModal();
  });
}

function initUploadPageNormal() {
  const fileInput = document.getElementById("fileInput");
  const dropArea = document.getElementById("dropArea");
  const selectedFile = document.getElementById("selectedFile");
  const fileNameEl = document.getElementById("fileName");
  const submitBtn = document.getElementById("submitBtn");
  const form = document.getElementById("uploadForm");

  if (!fileInput || !dropArea || !form) return;

  const showFile = (file) => {
    if (fileNameEl) fileNameEl.textContent = file.name;
    if (selectedFile) selectedFile.style.display = "flex";
    dropArea.style.display = "none";
    if (submitBtn) submitBtn.disabled = false;
  };

  window.clearFile = () => {
    fileInput.value = "";
    if (selectedFile) selectedFile.style.display = "none";
    dropArea.style.display = "flex";
    if (submitBtn) submitBtn.disabled = true;
  };

  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) showFile(fileInput.files[0]);
  });

  dropArea.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropArea.classList.add("drag-over");
  });

  dropArea.addEventListener("dragleave", () => {
    dropArea.classList.remove("drag-over");
  });

  dropArea.addEventListener("drop", (event) => {
    event.preventDefault();
    dropArea.classList.remove("drag-over");
    const file = event.dataTransfer.files[0];
    if (!file) return;
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
    showFile(file);
  });

  form.addEventListener("submit", () => {
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "⏳ Importing… this may take a few seconds";
    }
  });
}

function wireUploadPagePaywall() {
  if (isUploadProBypassed()) {
    initUploadPageNormal();
    return;
  }

  const fileInput = document.getElementById("fileInput");
  const dropArea = document.getElementById("dropArea");
  const form = document.getElementById("uploadForm");
  const browseLabel = dropArea?.querySelector(".upload-browse");

  const blockUpload = (event) => {
    event.preventDefault();
    event.stopPropagation();
    openSubscriptionModal();
  };

  fileInput?.addEventListener("click", blockUpload);
  fileInput?.addEventListener("change", blockUpload);
  browseLabel?.addEventListener("click", blockUpload);

  dropArea?.addEventListener("click", (event) => {
    if (event.target === fileInput || event.target === browseLabel) return;
    blockUpload(event);
  });

  dropArea?.addEventListener("dragover", (event) => event.preventDefault());
  dropArea?.addEventListener("drop", blockUpload);
  form?.addEventListener("submit", blockUpload);

  openSubscriptionModal();
}

document.addEventListener("DOMContentLoaded", initSubscriptionModal);
