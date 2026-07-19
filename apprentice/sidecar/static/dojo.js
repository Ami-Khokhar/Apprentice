(() => {
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
