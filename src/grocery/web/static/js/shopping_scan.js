// Scan a barcode for the shopping list: one scan, added if the barcode is known, then the camera stops.
(function () {
  var GA = window.GA || {};
  var $ = function (id) { return document.getElementById(id); };
  if (!$("sl-start")) return;

  var toastTimer = null;
  function toast(text, kind) {
    var el = $("sl-toast");
    el.textContent = text;
    el.className = "toast " + (kind || "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, kind === "bad" ? 4000 : 2500);
  }

  function stopCamera() {
    GA.scanner.stop();
    $("sl-scanner").hidden = true; $("sl-start").hidden = false; $("sl-stop").hidden = true;
  }

  function token() {
    return fetch("/api/csrf", { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json().then(function (j) { return j.token; }) : null; });
  }

  function send(ean) {
    stopCamera();
    toast("Barcode " + ean + " seen, looking it up...");
    return token().then(function (t) {
      if (!t) { toast("You are signed out. Sign in again and continue.", "bad"); return; }
      return fetch("/api/shopping-list/scan", {
        method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": t, "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
        body: new URLSearchParams({ ean: ean }),
      }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, data: j }; }); })
        .then(function (res) {
          if (!res.ok) { toast(res.data.error || "Could not add that barcode.", "bad"); return; }
          toast("Added: " + res.data.name, "good");
          setTimeout(function () { window.location.href = "/shopping-list"; }, 900);
        });
    }).catch(function () { toast("No connection. Try again.", "bad"); });
  }

  $("sl-start").addEventListener("click", function () {
    $("sl-scanner").hidden = false; $("sl-start").hidden = true; $("sl-stop").hidden = false;
    GA.scanner.start($("sl-video"), send, function (message) {
      toast(message, "bad"); $("sl-scanner").hidden = true; $("sl-start").hidden = false; $("sl-stop").hidden = true;
    });
  });
  $("sl-stop").addEventListener("click", stopCamera);
})();
