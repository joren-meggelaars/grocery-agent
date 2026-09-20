// Reloads the page once the receipt has been read (status changes away from "extracting").
(function () {
  var box = document.querySelector("[data-poll-url]");
  if (!box) return;
  var url = box.getAttribute("data-poll-url");
  var current = box.getAttribute("data-poll-status");
  var started = Date.now();

  function tick() {
    if (Date.now() - started > 10 * 60 * 1000) return; // stop after 10 minutes
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && data.status !== current) { window.location.reload(); return; }
        setTimeout(tick, 2000);
      })
      .catch(function () { setTimeout(tick, 4000); });
  }
  setTimeout(tick, 2000);
})();
