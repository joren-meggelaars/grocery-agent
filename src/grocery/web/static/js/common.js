// Any form with data-confirm asks for confirmation before it is submitted.
(function () {
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (event) {
      if (!window.confirm(f.getAttribute("data-confirm"))) event.preventDefault();
    });
  });
})();

// Offline support for the capture page (see sw.js).
if ("serviceWorker" in navigator) {
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () { /* unsupported or blocked */ });
  });
}
