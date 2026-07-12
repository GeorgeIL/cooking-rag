/**
 * Shared auth form handler.
 * Submits the form as JSON and redirects on success.
 */
function setupAuthForm(formId, url, submitBtnId, alertId) {
  const form = document.getElementById(formId);
  const submitBtn = document.getElementById(submitBtnId);
  const alertEl = document.getElementById(alertId);

  function showAlert(msg, type = "error") {
    alertEl.textContent = msg;
    alertEl.className = `alert alert--${type}`;
    alertEl.style.display = "block";
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    alertEl.style.display = "none";

    // Collect form fields into a plain object
    const data = {};
    new FormData(form).forEach((v, k) => {
      data[k] = v;
    });

    submitBtn.disabled = true;
    submitBtn.textContent = "Please wait…";

    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      const json = await resp.json();

      if (resp.ok) {
        showAlert(json.message || "Success", "success");
        if (json.redirect) window.location.href = json.redirect;
      } else {
        showAlert(json.error || "Something went wrong");
      }
    } catch {
      showAlert("Network error — please try again");
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = submitBtn.dataset.originalText || "Submit";
    }
  });

  // Store original button text for reset
  submitBtn.dataset.originalText = submitBtn.textContent;
}
