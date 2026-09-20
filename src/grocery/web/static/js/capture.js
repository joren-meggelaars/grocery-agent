// The in-store capture page: pick store, scan or type a barcode, photograph the label, save to the outbox.
(function () {
  var GA = window.GA || {};
  var $ = function (id) { return document.getElementById(id); };
  if (!$("save")) return;

  var photoBlob = null;
  var storeSel = $("store"), eanInput = $("ean"), msg = $("msg"), scanMsg = $("scan-msg");

  function lsGet(key, fallback) { try { var v = localStorage.getItem(key); return v === null ? fallback : v; } catch (e) { return fallback; } }
  function lsSet(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } }

  // Remember the last used store and the review preference.
  var lastStore = lsGet("ga.store", "");
  if (lastStore && Array.prototype.some.call(storeSel.options, function (o) { return o.value === lastStore; })) storeSel.value = lastStore;
  storeSel.addEventListener("change", function () { lsSet("ga.store", storeSel.value); });
  $("review-now").checked = lsGet("ga.reviewNow", "1") === "1";
  $("review-now").addEventListener("change", function () { lsSet("ga.reviewNow", this.checked ? "1" : "0"); });

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var b = new Uint8Array(16); crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    var h = Array.prototype.map.call(b, function (x) { return ("0" + x.toString(16)).slice(-2); }).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }

  // --- barcode -------------------------------------------------------------
  $("scan-start").addEventListener("click", function () {
    $("scanner").hidden = false; scanMsg.textContent = "Starting the camera...";
    GA.scanner.start($("scan-video"), function (digits) {
      eanInput.value = digits; $("scanner").hidden = true; scanMsg.textContent = "Barcode " + digits;
    }, function (message) { $("scanner").hidden = true; scanMsg.textContent = message; });
  });
  $("scan-stop").addEventListener("click", function () { GA.scanner.stop(); $("scanner").hidden = true; scanMsg.textContent = ""; });
  eanInput.addEventListener("input", function () {
    var v = eanInput.value.replace(/\D/g, "");
    scanMsg.textContent = v.length >= 8 && !GA.scanner.checkDigitOk(v) ? "That number does not look like a valid barcode." : "";
  });

  // --- photo ---------------------------------------------------------------
  function shrink(file) {
    return createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bitmap) {
      var scale = Math.min(1, 1400 / Math.max(bitmap.width, bitmap.height));
      var canvas = document.createElement("canvas");
      canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      return new Promise(function (resolve) { canvas.toBlob(function (b) { resolve(b || file); }, "image/jpeg", 0.82); });
    }).catch(function () { return file; });
  }
  $("label-photo").addEventListener("change", function () {
    var file = this.files && this.files[0];
    if (!file) return;
    shrink(file).then(function (blob) {
      photoBlob = blob;
      var img = $("preview");
      if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
      img.dataset.url = URL.createObjectURL(blob); img.src = img.dataset.url; img.hidden = false;
      $("save").disabled = false; msg.textContent = "";
    });
  });

  // --- save ----------------------------------------------------------------
  function reset() {
    photoBlob = null; eanInput.value = ""; scanMsg.textContent = "";
    $("label-photo").value = ""; $("preview").hidden = true; $("save").disabled = true;
  }

  $("save").addEventListener("click", function () {
    if (!photoBlob) return;
    var rec = {
      client_uuid: uuid(), ean: eanInput.value.replace(/\D/g, ""), store: storeSel.value,
      captured_at: new Date().toISOString(), photo: photoBlob,
    };
    $("save").disabled = true;
    GA.outbox.add(rec).then(function () {
      reset();
      msg.textContent = "Saved. Uploading...";
      return GA.outbox.flush();
    }).then(function (ids) {
      if (ids && ids.length) {
        msg.textContent = "Uploaded.";
        if ($("review-now").checked) window.location.href = "/capture/" + ids[ids.length - 1];
      } else {
        msg.textContent = "Saved on this phone. It uploads by itself when you have a connection.";
      }
    }).catch(function () {
      msg.textContent = "Could not save on this phone. Try again.";
      $("save").disabled = false;
    });
  });

  // --- outbox status -------------------------------------------------------
  document.addEventListener("ga:outbox", function (e) {
    var records = e.detail.records, box = $("outbox-status"), list = $("outbox-list");
    var waiting = records.filter(function (r) { return !r.error; }).length;
    $("pending-count").textContent = String(waiting);
    box.hidden = records.length === 0;
    $("login-link").hidden = !e.detail.needsLogin;
    list.hidden = records.filter(function (r) { return r.error; }).length === 0;
    list.textContent = "";
    records.filter(function (r) { return r.error; }).forEach(function (r) {
      var li = document.createElement("li"); li.className = "card";
      li.appendChild(document.createTextNode("Rejected: " + r.error + " "));
      var retry = document.createElement("button"); retry.type = "button"; retry.className = "btn"; retry.textContent = "Try again";
      retry.addEventListener("click", function () { GA.outbox.retry(r.client_uuid); });
      var drop = document.createElement("button"); drop.type = "button"; drop.className = "btn danger"; drop.textContent = "Discard";
      drop.addEventListener("click", function () { GA.outbox.remove(r.client_uuid); });
      li.appendChild(retry); li.appendChild(drop); list.appendChild(li);
    });
  });
})();
