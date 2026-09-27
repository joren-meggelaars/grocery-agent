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

// Phone layout: the "menu" buttons open and close the full navigation.
document.querySelectorAll("[data-nav-toggle]").forEach(function (button) {
  button.addEventListener("click", function () {
    var shell = document.querySelector(".shell");
    if (shell) shell.classList.toggle("nav-open");
  });
});

// The "Scan" tab button opens a small menu (Scan in store / Receipts) instead of navigating directly.
document.querySelectorAll("[data-scan-toggle]").forEach(function (button) {
  var pop = document.getElementById(button.getAttribute("aria-controls"));
  if (!pop) return;
  function close() { pop.hidden = true; button.setAttribute("aria-expanded", "false"); }
  function open() { pop.hidden = false; button.setAttribute("aria-expanded", "true"); }
  button.addEventListener("click", function (event) {
    event.stopPropagation();
    if (pop.hidden) open(); else close();
  });
  document.addEventListener("click", function (event) {
    if (!pop.hidden && event.target !== button && !pop.contains(event.target)) close();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") close();
  });
});
