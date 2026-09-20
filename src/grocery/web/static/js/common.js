// Any form with data-confirm asks for confirmation before it is submitted.
(function () {
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (event) {
      if (!window.confirm(f.getAttribute("data-confirm"))) event.preventDefault();
    });
  });
})();
