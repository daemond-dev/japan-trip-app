#!/usr/bin/env python3
"""Load app/index.html against the data folder with a stub claude.ai runtime and check every tab renders.
Prints one JSON report per color scheme (title, clock, tab text, console errors) and saves screenshots to /tmp.
Usage: python3 scripts/test_page.py [live|offline|static|sw] [--page app/index.html] [--data data]
  live (default): stub window.claude.use serving the data, as the published artifact would.
  offline: the Android offline copy (patched page + data.js + shim.js) with a stub window.TripApp.
  static: the same files as a plain website, no TripApp, as GitHub Pages serves them.
  sw: builds the static site, serves it on localhost and checks the service worker installs and the page reloads offline.
Exits non-zero when a page error or console error is seen. Needs Playwright with Chromium
(pip install playwright; python -m playwright install chromium)."""
import json, os, sys, glob, time
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
args = sys.argv[1:]
MODE = "offline" if "offline" in args else "static" if "static" in args else "sw" if "sw" in args else "live"
OFFLINE = MODE != "live"
PAGE = args[args.index("--page") + 1] if "--page" in args else f"{ROOT}/app/index.html"
DATA_DIR = args[args.index("--data") + 1] if "--data" in args else f"{ROOT}/data"
trip = {os.path.basename(f)[:-5]: json.load(open(f, encoding="utf-8")) for f in glob.glob(f"{DATA_DIR}/*.json")}
DATA = json.dumps({"trip": trip, "todo": []}, ensure_ascii=False)

LIVE_STUB = """
(function(){
  var D = %s;
  window.__writes = [];
  function clone(x){ return x === undefined ? undefined : JSON.parse(JSON.stringify(x)); }
  function snap(id, data){ return {id:id, exists: !!data, data: function(){ return clone(data); }, metadata:{fromCache:false, hasPendingWrites:false}}; }
  function qsnap(){ var docs = D.todo.map(function(t){ var d = Object.assign({}, t), id = d.id; delete d.id; return snap(id, d); }); return {docs:docs, size:docs.length, empty:!docs.length, docChanges:function(){return [];}, metadata:{fromCache:false}}; }
  var db = {
    doc: function(path){ var k = String(path).split("/")[1], c = String(path).split("/")[0];
      return { onSnapshot: function(next){ setTimeout(function(){ next(snap(k, c === "trip" ? D.trip[k] : undefined)); }, 0); return function(){}; },
               get: function(){ return Promise.resolve(snap(k, c === "trip" ? D.trip[k] : undefined)); },
               set: function(v){ window.__writes.push({op:"set", path:path, v:v}); if(c === "trip") D.trip[k] = v; return Promise.resolve(); },
               update: function(v){ window.__writes.push({op:"update", path:path, v:v}); return Promise.resolve(); },
               delete: function(){ window.__writes.push({op:"delete", path:path}); return Promise.resolve(); } }; },
    collection: function(){ var q = { orderBy: function(){ return q; }, where: function(){ return q; }, limit: function(){ return q; },
      get: function(){ return Promise.resolve(qsnap()); }, onSnapshot: function(next){ setTimeout(function(){ next(qsnap()); }, 0); return function(){}; },
      doc: function(id){ return { update: function(v){ window.__writes.push({op:"todo", id:id, v:v}); return Promise.resolve(); }, set: function(v){ window.__writes.push({op:"todo-set", id:id, v:v}); return Promise.resolve(); } }; } }; return q; }
  };
  var user = { can: function(){ return Promise.resolve(true); }, isOwner: function(){ return Promise.resolve(true); } };
  window.claude = { use: function(n){ return Promise.resolve(n === "db" ? db : n === "user" ? user : null); } };
})();
""" % DATA

APK_STUB = """
(function(){
  var local = {};
  var log = [];
  window.__taLog = log;
  window.TripApp = {
    saveData: function(s){ log.push("saveData " + s.length); }, getData: function(){ return null; },
    getLocal: function(k){ return local[k] || null; }, saveLocal: function(k, v){ local[k] = v; },
    takePending: function(){ return "[]"; }, addPending: function(){}, setBright: function(){}, saveDoc: function(){}, getDoc: function(){ return null; }, docCount: function(){ return 0; },
    copy: function(){}, netState: function(){ return "none"; }, isOnline: function(){ return false; }, goLive: function(){ log.push("goLive"); },
    pageVersion: function(){ return "1"; }, savePage: function(){}, liveReady: function(){ log.push("liveReady"); }, setWantsBack: function(){}, ackBack: function(){},
    getLocation: function(){ return null; }, openTranslate: function(){ log.push("openTranslate"); }, setChrome: function(){}, saveFile: function(){}, paste: function(){ return ""; }
  };
})();
"""

def sw_test():
    """Build the site, serve it, register the worker, then reload with the network off."""
    import http.server, socketserver, threading, subprocess, tempfile
    out = tempfile.mkdtemp(prefix="site-")
    subprocess.run([sys.executable, f"{ROOT}/scripts/build_static.py", DATA_DIR, out], check=True)
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
    handler = lambda *a, **k: Quiet(*a, directory=out, **k)
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    failed = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(); ctx = b.new_context(viewport={"width": 390, "height": 844}); pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        pg.on("console", lambda m: errors.append("console.error: " + m.text) if m.type == "error" else None)
        pg.goto(base + "index.html"); pg.wait_for_timeout(1500)
        state = pg.evaluate("navigator.serviceWorker.ready.then(r => r.active && r.active.state)")
        pg.wait_for_timeout(1500)
        keys = pg.evaluate("caches.keys()")
        cached = pg.evaluate("caches.keys().then(ks => caches.open(ks[0])).then(c => c.keys()).then(k => k.length)") if keys else 0
        ctx.set_offline(True)
        pg.goto(base); pg.wait_for_timeout(1500)
        title = pg.title(); pg.click('.tabs button[data-tab="places"]'); pg.wait_for_timeout(500)
        cards = pg.locator("main .card, main .place").count()
        ctx.set_offline(False); b.close()
    srv.shutdown()
    report = {"mode": "sw", "worker": state, "caches": keys, "cached_files": cached, "offline_title": title, "offline_places": cards, "errors": errors}
    print(json.dumps(report, indent=1))
    if state != "activated" or cached < 7 or cards < 1 or errors: failed.append(report)
    return failed

def main():
    if MODE == "sw":
        sys.exit(1 if sw_test() else 0)
    failed = []
    page_path = PAGE
    if OFFLINE:
        os.makedirs("/tmp/offline", exist_ok=True)
        os.system(f"python3 {ROOT}/scripts/patch_html.py {page_path} /tmp/offline/index.html && python3 {ROOT}/scripts/make_data.py {DATA_DIR} /tmp/offline/data.js && cp {ROOT}/android/assets/www/shim.js /tmp/offline/shim.js")
        page_path = "/tmp/offline/index.html"
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        for scheme in ("light", "dark"):
            ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme=scheme, timezone_id="Asia/Tokyo", locale="en-US")
            pg = ctx.new_page()
            errors = []
            pg.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
            pg.on("console", lambda m: errors.append("console." + m.type + ": " + m.text) if m.type in ("error", "warning") else None)
            if MODE == "offline": pg.add_init_script(APK_STUB)
            elif MODE == "live": pg.add_init_script(LIVE_STUB)
            pg.goto("file://" + os.path.abspath(page_path))
            pg.wait_for_timeout(1500)
            out = {"scheme": scheme}
            out["title"] = pg.title()
            out["h1"] = pg.locator("#appTitle").inner_text()
            out["clock"] = pg.locator("#clock").inner_text()
            out["today_text"] = pg.locator("main").inner_text()[:400].replace("\n", " | ")
            pg.screenshot(path=f"/tmp/shot-{scheme}-{MODE}-today.png")
            def tab(name):
                pg.click(f'.tabs button[data-tab="{name}"]'); pg.wait_for_timeout(500)
            pg.evaluate("location.hash = '#reservations'"); pg.wait_for_timeout(600); out["res"] = pg.locator("main").inner_text()[:200].replace("\n", " | ")
            tab("places"); out["places_cards"] = pg.locator("main .card, main .place").count(); out["places_text"] = pg.locator("main").inner_text()[:160].replace("\n", " | ")
            pg.screenshot(path=f"/tmp/shot-{scheme}-{MODE}-places.png")
            tab("yen"); out["yen"] = pg.locator("main").inner_text()[:160].replace("\n", " | ")
            tab("speak"); pg.wait_for_timeout(600)
            out["speak_themes"] = pg.locator("main button").all_inner_texts()[:40]
            pg.screenshot(path=f"/tmp/shot-{scheme}-{MODE}-speak.png")
            inp = pg.locator('#q')
            inp.fill("stroller"); pg.wait_for_timeout(700)
            out["search_stroller"] = pg.locator("main").inner_text()[:500].replace("\n", " | ")
            inp.fill("seafood"); pg.wait_for_timeout(700)
            out["search_seafood"] = pg.locator("main").inner_text()[:400].replace("\n", " | ")
            inp.fill("reservation"); pg.wait_for_timeout(700)
            out["search_reservation"] = pg.locator("main").inner_text()[:300].replace("\n", " | ")
            inp.fill(""); pg.wait_for_timeout(500)
            # menu and trip overview
            pg.click("#menuBtn"); pg.wait_for_timeout(400)
            out["drawer"] = pg.locator("#drawer").inner_text()[:300].replace("\n", " | ")
            pg.screenshot(path=f"/tmp/shot-{scheme}-{MODE}-drawer.png")
            out["errors"] = errors
            out["writes"] = pg.evaluate("window.__writes ? window.__writes.length : null")
            print(json.dumps(out, ensure_ascii=False, indent=1))
            if errors: failed.append(scheme)
            ctx.close()
        b.close()
    if failed:
        print("FAILED:", failed); sys.exit(1)

main()
