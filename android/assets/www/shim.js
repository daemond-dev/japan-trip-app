/* Offline copy inside the Android app: stands in for the claude.ai runtime.
   Data is the last copy the live app handed over (window.TripApp.getData), or the copy bundled at build time.
   To-do ticks made here are queued and sent to the live app the next time it opens. */
(function(){
  var TA = window.TripApp || null;
  var bundled = window.__TRIP_DATA || {trip:{}, todo:[], built:""};
  var saved = null;
  try{ var s = TA && TA.getData(); if(s) saved = JSON.parse(s); }catch(e){ saved = null; }
  function jst(ms){
    var d = new Date(ms + 9 * 3600e3), MO = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return MO[d.getUTCMonth()] + " " + d.getUTCDate() + ", " + String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0") + " Japan time";
  }
  var savedAt = saved && saved.savedAt ? +saved.savedAt : 0;
  /* Static host: this page IS the live URL (a plain website such as GitHub Pages) opened inside the Android app, or the
     same site in a browser with no app at all. Its bundled data is the freshest there is, so use it and, in the app, hand
     it over so the offline copy shows it next time. */
  var staticHost = !TA; try{ if(TA && TA.isStaticHost && TA.isStaticHost()) staticHost = true; }catch(e){}
  var D = !staticHost && saved && saved.trip && saved.trip.days
    ? {trip: saved.trip, todo: (saved.todo || []), built: jst(savedAt || Date.now())}
    : bundled;
  if(staticHost && TA && bundled.trip && bundled.trip.days){ try{ TA.saveData(JSON.stringify({savedAt: Date.now(), trip: bundled.trip, todo: bundled.todo || []})); savedAt = Date.now(); }catch(e){} }
  window.__APK = {built: D.built, offline: true, static: staticHost};
  /* Ticks made offline sit on top of the saved list only until the live app has saved newer data (which includes them
     once they were sent), so a change made elsewhere isn't hidden behind a stale local tick. */
  var OV = "jt-apk-todo";
  function ov(){
    var o; try{ o = JSON.parse(localStorage.getItem(OV) || "{}") || {}; }catch(e){ o = {}; }
    var out = {}, changed = false;
    Object.keys(o).forEach(function(id){ var e = o[id]; if(e && typeof e === "object" && "at" in e){ if(e.at >= savedAt) out[id] = e; else changed = true; } else changed = true; });
    if(changed){ try{ localStorage.setItem(OV, JSON.stringify(out)); }catch(e){} }
    return out;
  }
  function clone(x){ return x === undefined ? undefined : JSON.parse(JSON.stringify(x)); }
  function snap(id, data){ return {id:id, exists: !!data, data: function(){ return clone(data); }, metadata:{fromCache:false, hasPendingWrites:false}}; }
  function todos(){
    var o = ov();
    return D.todo.map(function(t){ return Object.assign({}, t, o[t.id] ? o[t.id].v : {}); }).sort(function(a, b){ return (a.order || 0) - (b.order || 0); });
  }
  function qsnap(){
    var docs = todos().map(function(t){ var d = Object.assign({}, t), id = d.id; delete d.id; return snap(id, d); });
    return {docs: docs, size: docs.length, empty: !docs.length, docChanges: function(){ return []; }, metadata:{fromCache:false}};
  }
  var listeners = [];
  var offline = function(){ return Promise.reject({code:"unavailable", message:"offline copy"}); };
  var db = {
    doc: function(path){
      var k = String(path).split("/")[1];
      return {
        onSnapshot: function(next){ setTimeout(function(){ next(snap(k, D.trip[k])); }, 0); return function(){}; },
        get: function(){ return Promise.resolve(snap(k, D.trip[k])); },
        set: offline, update: offline, delete: offline
      };
    },
    collection: function(){
      var q = {
        orderBy: function(){ return q; }, where: function(){ return q; }, limit: function(){ return q; },
        get: function(){ return Promise.resolve(qsnap()); },
        onSnapshot: function(next){
          listeners.push(next); setTimeout(function(){ next(qsnap()); }, 0);
          return function(){ listeners = listeners.filter(function(f){ return f !== next; }); };
        },
        doc: function(id){
          return { update: function(v){
            var o = ov(); o[id] = {v: Object.assign((o[id] && o[id].v) || {}, v), at: Date.now()};
            try{ localStorage.setItem(OV, JSON.stringify(o)); }catch(e){}
            try{ if(TA) TA.addPending(JSON.stringify({id: id, data: v})); }catch(e){}
            setTimeout(function(){ listeners.forEach(function(f){ f(qsnap()); }); }, 0);
            return Promise.resolve();
          } };
        }
      };
      return q;
    }
  };
  var user = { can: function(){ return Promise.resolve(true); }, isOwner: function(){ return Promise.resolve(true); } };
  window.claude = { use: function(n){ return Promise.resolve(n === "db" ? db : n === "user" ? user : null); } };

  /* Android back button, in case the page hasn't defined its own (it normally does: appBack). */
  if(!window.__apkBack) window.__apkBack = function(){
    var show = document.getElementById("show");
    if(show && !show.hidden){ document.getElementById("showClose").click(); return true; }
    var dr = document.getElementById("drawer");
    if(dr && dr.classList.contains("open")){ document.getElementById("drawerClose").click(); return true; }
    var cur = document.querySelector('.tabs button[aria-current="page"]');
    if(!cur || cur.dataset.tab !== "today"){ var t = document.querySelector('.tabs button[data-tab="today"]'); if(t){ t.click(); return true; } }
    return false;
  };
})();
