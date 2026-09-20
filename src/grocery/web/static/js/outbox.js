// Offline outbox: captures (photo + barcode) are stored in IndexedDB first and uploaded when possible.
// Uploading always goes through here, so a flaky connection in the shop never loses a capture.
(function () {
  var GA = (window.GA = window.GA || {});
  var DB_NAME = "grocery-agent";
  var STORE = "captures";
  var dbPromise = null;
  var queue = Promise.resolve();

  function open() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = function () {
        req.result.createObjectStore(STORE, { keyPath: "client_uuid" });
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
    return dbPromise;
  }

  function tx(mode, fn) {
    return open().then(function (db) {
      return new Promise(function (resolve, reject) {
        var t = db.transaction(STORE, mode);
        var result = fn(t.objectStore(STORE));
        t.oncomplete = function () { resolve(result && result.result !== undefined ? result.result : undefined); };
        t.onerror = function () { reject(t.error); };
        t.onabort = function () { reject(t.error); };
      });
    });
  }

  function all() { return tx("readonly", function (s) { return s.getAll(); }).then(function (r) { return r || []; }); }
  function put(rec) { return tx("readwrite", function (s) { return s.put(rec); }); }
  function remove(uuid) { return tx("readwrite", function (s) { return s.delete(uuid); }); }

  function notify() {
    all().then(function (records) {
      document.dispatchEvent(new CustomEvent("ga:outbox", { detail: { records: records, needsLogin: GA.needsLogin === true } }));
    }).catch(function () {});
  }

  function csrfToken() {
    return fetch("/api/csrf", { credentials: "same-origin", headers: { Accept: "application/json" } }).then(function (r) {
      if (r.status === 401 || r.status === 403 || r.redirected || r.status === 303) { GA.needsLogin = true; return null; }
      if (!r.ok) return null;
      return r.json().then(function (j) { GA.needsLogin = false; return j.token; });
    });
  }

  function upload(rec, token) {
    var form = new FormData();
    form.append("client_uuid", rec.client_uuid);
    form.append("ean", rec.ean || "");
    form.append("store", rec.store);
    form.append("captured_at", rec.captured_at);
    form.append("photo", rec.photo, "label.jpg");
    return fetch("/api/captures", {
      method: "POST", body: form, credentials: "same-origin", headers: { "X-CSRF-Token": token },
    });
  }

  // One pass over everything that is waiting. Resolves with the capture ids that were created or already known.
  function doFlush() {
    var done = [];
    return all().then(function (records) {
      var pending = records.filter(function (r) { return !r.error; })
        .sort(function (a, b) { return a.captured_at < b.captured_at ? -1 : 1; });
      if (!pending.length) return null;
      return csrfToken().then(function (token) {
        if (!token) return null;
        var chain = Promise.resolve(), stop = false;
        pending.forEach(function (rec) {
          chain = chain.then(function () {
            if (stop) return;
            return upload(rec, token).then(function (resp) {
              if (resp.ok) {
                return resp.json().then(function (j) { done.push(j.id); return remove(rec.client_uuid); });
              }
              if (resp.status === 400) {
                return resp.json().catch(function () { return {}; }).then(function (j) {
                  rec.error = j.error || "Rejected by the server";
                  return put(rec);
                });
              }
              if (resp.status === 401 || resp.status === 403) { GA.needsLogin = true; }
              stop = true; // stop on auth problems / server errors; try again later
            });
          }).catch(function () { stop = true; }); // network error: try again later
        });
        return chain;
      });
    }).then(function () { notify(); return done; },
            function () { notify(); return done; });
  }

  // Passes run one after another, so a capture saved while an upload is running is picked up by the next pass.
  function flush() {
    queue = queue.then(doFlush, doFlush);
    return queue;
  }

  GA.outbox = {
    add: function (rec) {
      return put(rec).then(function () {
        if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(function () {});
        notify();
      });
    },
    all: all,
    remove: function (uuid) { return remove(uuid).then(notify); },
    retry: function (uuid) {
      return all().then(function (records) {
        var rec = records.filter(function (r) { return r.client_uuid === uuid; })[0];
        if (!rec) return;
        delete rec.error;
        return put(rec).then(flush);
      });
    },
    flush: flush,
  };

  window.addEventListener("online", flush);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) flush(); });
  window.addEventListener("load", function () { flush(); });
})();
