(() => {
  let apiKey = "";
  const apiKeyForm = document.querySelector("[data-api-key-form]");
  const apiKeyStatus = document.querySelector("[data-api-key-status]");

  if (apiKeyForm) {
    const input = apiKeyForm.querySelector('input[name="api_key"]');
    apiKeyForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const candidate = input.value.trim();
      if (!/^[A-Za-z0-9_.-]{20,200}$/.test(candidate)) {
        input.setCustomValidity("Enter a valid OpenAI API key.");
        input.reportValidity();
        return;
      }
      input.setCustomValidity("");
      apiKey = candidate;
      input.value = "";
      if (apiKeyStatus) apiKeyStatus.textContent = "Key ready for this page.";
      apiKeyForm.closest("details")?.removeAttribute("open");
    });
  }

  document.querySelectorAll("form[data-api-key-required]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!apiKey) {
        event.preventDefault();
        apiKeyForm?.closest("details")?.setAttribute("open", "");
        apiKeyForm?.querySelector('input[name="api_key"]')?.focus();
        if (apiKeyStatus) apiKeyStatus.textContent = "Add your API key to begin.";
        return;
      }
      const keyField = document.createElement("input");
      keyField.type = "hidden";
      keyField.name = "api_key";
      keyField.value = apiKey;
      form.append(keyField);
    });
  });

  const loadingScreen = document.querySelector("#loading-screen");
  const main = document.querySelector("#main-content");
  const mainSurface = document.querySelector("[data-main-surface]");

  if (!loadingScreen || !main || !mainSurface) return;

  const loadingTitle = loadingScreen.querySelector("[data-loading-title]");
  const loadingDetail = loadingScreen.querySelector("[data-loading-detail]");

  const resetLoadingState = () => {
    document.body.classList.remove("is-loading");
    loadingScreen.hidden = true;
    main.removeAttribute("aria-busy");
    mainSurface.removeAttribute("inert");

    document.querySelectorAll("form[data-submitting]").forEach((form) => {
      delete form.dataset.submitting;
      form.removeAttribute("aria-busy");
      form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach((control) => {
        control.disabled = false;
      });
    });
  };

  document.querySelectorAll("form[data-loading-title]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      if (form.dataset.submitting === "true") {
        event.preventDefault();
        return;
      }

      form.dataset.submitting = "true";
      form.setAttribute("aria-busy", "true");
      main.setAttribute("aria-busy", "true");
      mainSurface.setAttribute("inert", "");

      if (loadingTitle) loadingTitle.textContent = form.dataset.loadingTitle;
      if (loadingDetail) loadingDetail.textContent = form.dataset.loadingDetail || "Take one steady breath.";

      loadingScreen.hidden = false;
      document.body.classList.add("is-loading");

      form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach((control) => {
        control.disabled = true;
      });
    });
  });

  window.addEventListener("pageshow", resetLoadingState);
})();
