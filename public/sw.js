/* Caches the app shell so the list opens with no signal at all.
   Without this, a cold open in a dead zone fails to load the Firebase SDK,
   the app silently falls back to device-only storage, and those changes
   never reach the shared list. */

var CACHE = "market-run-v1";

var SHELL = [
  "./",
  "./index.html",
  "./icon.svg",
  "./apple-touch-icon.png",
  "./manifest.webmanifest",
  "https://www.gstatic.com/firebasejs/12.19.0/firebase-app-compat.js",
  "https://www.gstatic.com/firebasejs/12.19.0/firebase-firestore-compat.js"
];

self.addEventListener("install", function(e){
  e.waitUntil(
    caches.open(CACHE).then(function(c){
      /* fetch + put rather than add(): add() rejects the opaque responses that
         cross-origin requests can return, which would drop the Firebase SDK.
         One bad URL shouldn't fail the whole install either. */
      return Promise.all(SHELL.map(function(u){
        return fetch(u, {cache: "reload"}).then(function(res){
          if(res && (res.ok || res.type === "opaque")) return c.put(u, res);
        }).catch(function(){});
      }));
    }).then(function(){ return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function(e){
  e.waitUntil(
    caches.keys().then(function(keys){
      return Promise.all(keys.map(function(k){
        return k === CACHE ? null : caches.delete(k);
      }));
    }).then(function(){ return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function(e){
  var req = e.request;
  if(req.method !== "GET") return;

  var url = new URL(req.url);
  var host = url.hostname;

  /* Firestore runs its own offline queue and long-lived streams — intercepting
     that traffic would break sync. Only the shell and fonts are ours. */
  var ours = url.origin === self.location.origin ||
             host === "fonts.googleapis.com" ||
             host === "fonts.gstatic.com" ||
             host === "www.gstatic.com";
  if(!ours) return;

  /* The page: network first, so a new version lands the moment there's signal. */
  if(req.mode === "navigate" || url.pathname === "/" || /\/index\.html$/.test(url.pathname)){
    e.respondWith(
      fetch(req).then(function(res){
        var copy = res.clone();
        caches.open(CACHE).then(function(c){ c.put(req, copy); });
        return res;
      }).catch(function(){
        return caches.match(req).then(function(hit){
          return hit || caches.match("./index.html");
        });
      })
    );
    return;
  }

  /* Fonts, icons and the pinned SDK never change: cache first. */
  e.respondWith(
    caches.match(req).then(function(hit){
      return hit || fetch(req).then(function(res){
        if(res && (res.ok || res.type === "opaque")){
          var copy = res.clone();
          caches.open(CACHE).then(function(c){ c.put(req, copy); });
        }
        return res;
      });
    })
  );
});
