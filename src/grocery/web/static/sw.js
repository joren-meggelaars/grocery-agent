// Service worker: keeps the capture page and its assets available offline.
// It never caches API responses or any page with personal data; /capture is identical for everyone.
var VERSION = "v1";
var CACHE = "ga-shell-" + VERSION;
var PAGE = "/capture";

function assetsIn(html) {
  var urls = [], re = /(?:src|href)="(\/static\/[^"]+)"/g, m;
  while ((m = re.exec(html))) urls.push(m[1].replace(/&amp;/g, "&"));
  return urls;
}

function cacheAssets(cache, html) {
  return Promise.all(assetsIn(html).map(function (u) { return cache.add(u).catch(function () {}); }));
}

function storePage(response) {
  var copy = response.clone();
  return caches.open(CACHE).then(function (cache) {
    return copy.text().then(function (html) {
      return cache.put(PAGE, new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } }))
        .then(function () { return cacheAssets(cache, html); });
    });
  });
}

self.addEventListener("install", function (event) {
  event.waitUntil(
    fetch(PAGE, { credentials: "same-origin" }).then(function (r) {
      // Not signed in yet (redirect to /login): nothing to cache now; the first visit will do it.
      if (r.ok && !r.redirected) return storePage(r);
    }).catch(function () {}).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname === PAGE && req.mode === "navigate") {
    event.respondWith(
      fetch(req).then(function (response) {
        if (response.ok && !response.redirected) event.waitUntil(storePage(response));
        return response; // a redirect (session expired) goes to the browser untouched
      }).catch(function () {
        return caches.match(PAGE).then(function (cached) {
          return cached || new Response("You are offline and the capture page has not been saved yet. Open it once with a connection.",
            { status: 503, headers: { "Content-Type": "text/plain" } });
        });
      })
    );
    return;
  }

  if (url.pathname.indexOf("/static/") === 0) {
    event.respondWith(
      caches.match(req).then(function (hit) {
        if (hit) return hit;
        return fetch(req).then(function (response) {
          if (response.ok) { var copy = response.clone(); caches.open(CACHE).then(function (c) { c.put(req, copy); }); }
          return response;
        });
      })
    );
  }
});
