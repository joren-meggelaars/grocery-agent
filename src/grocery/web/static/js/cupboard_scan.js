// Scan products at home: the camera stops at the first barcode. A known product is added and listed; a new one
// is looked up and you go straight to naming it.
(function () {
  var GA = window.GA || {};
  var $ = function (id) { return document.getElementById(id); };
  if (!$("cb-start")) return;

  var added = 0, waiting = parseInt($("cb-waiting").textContent, 10) || 0;

  // A toast over the camera view: while a product is held in front of the lens the list below is out of sight.
  var toastTimer = null;
  function toast(text, kind) {
    var el = $("cb-toast");
    el.textContent = text; // text only, like the list
    el.className = "toast " + (kind || "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, kind === "bad" ? 4000 : 2500);
  }

  function show(text, kind) {
    toast(text, kind);
    var li = document.createElement("li");
    li.className = "card scan-result " + (kind || "");
    li.textContent = text; // text only: names come from the database or Open Food Facts
    var log = $("cb-log");
    log.insertBefore(li, log.firstChild);
    while (log.children.length > 15) log.removeChild(log.lastChild);
  }

  function counters() { $("cb-added").textContent = String(added); $("cb-waiting").textContent = String(waiting); }

  function token() {
    return fetch("/api/csrf", { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json().then(function (j) { return j.token; }) : null; });
  }

  function stopCamera() {
    GA.scanner.stop();
    $("cb-scanner").hidden = true; $("cb-start").hidden = false; $("cb-stop").hidden = true;
  }

  // A new barcode is looked up by the worker in the background; give it a moment so the name is already filled in.
  function goToNaming(scanId) {
    var started = Date.now();
    (function check() {
      fetch("/cupboard/name/" + scanId + "/status", { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if ((data && data.status !== "lookup") || Date.now() - started > 6000) {
            window.location.href = "/cupboard/name?scan=" + scanId;
          } else { setTimeout(check, 700); }
        })
        .catch(function () { window.location.href = "/cupboard/name?scan=" + scanId; });
    })();
  }

  function send(ean) {
    stopCamera(); // one barcode at a time: the camera is off as soon as something was read
    toast("Barcode " + ean + " seen, looking it up...");
    return token().then(function (t) {
      if (!t) { show("You are signed out. Sign in again and continue.", "bad"); return; }
      return fetch("/api/cupboard/scan", {
        method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": t, "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
        body: new URLSearchParams({ ean: ean }),
      }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, data: j }; }); })
        .then(function (res) {
          if (!res.ok) { show((res.data && res.data.error) || "Could not add that barcode.", "bad"); return; }
          var kind = res.data.result, name = res.data.name;
          if (kind === "added") { added += 1; show("Found: " + name, "good"); }
          else if (kind === "already") { show("Already on the list: " + name); }
          else if (kind === "unknown") { waiting += 1; show("New barcode " + ean + ": looking up the name..."); }
          else { show("Barcode " + ean + " is still waiting for a name."); }
          counters();
          if ((kind === "unknown" || kind === "unknown_again") && res.data.scan_id) goToNaming(res.data.scan_id);
        });
    }).catch(function () { show("No connection. Try that one again.", "bad"); });
  }

  $("cb-start").addEventListener("click", function () {
    $("cb-scanner").hidden = false; $("cb-start").hidden = true; $("cb-stop").hidden = false;
    GA.scanner.start($("cb-video"), send, function (message) {
      show(message, "bad"); $("cb-scanner").hidden = true; $("cb-start").hidden = false; $("cb-stop").hidden = true;
    });
  });
  $("cb-stop").addEventListener("click", stopCamera);
  $("cb-add").addEventListener("click", function () {
    var v = $("cb-ean").value.replace(/\D/g, "");
    if (v) { send(v).then(function () { $("cb-ean").value = ""; }); }
  });
  counters();
})();
