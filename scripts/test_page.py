#!/usr/bin/env python3
"""Load app/index.html against the data folder with a stub claude.ai runtime and check every tab renders.
Prints one JSON report per color scheme (title, clock, tab text, console errors) and saves screenshots to /tmp.
On the first start the page opens "Set up this copy"; the harness fills it (home time zone, dates around today, an added
city) and then checks the phone's own days, places and "+ Day" (see docs/NOTES.md, "Set up this copy").
Usage: python3 scripts/test_page.py [live|offline|static|sw] [--page app/index.html] [--data data]
  live (default): stub window.claude.use serving the data, as the published artifact would.
  offline: the Android offline copy (patched page + data.js + shim.js) with a stub window.TripApp.
  static: the same files as a plain website, no TripApp, as GitHub Pages serves them.
  sw: builds the static site, serves it on localhost and checks the service worker installs and the page reloads offline.
Exits non-zero when a page error or console error is seen. Needs Playwright with Chromium
(pip install playwright; python -m playwright install chromium)."""
import json, os, sys, glob, time, re, datetime
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
        b = pw.chromium.launch(); ctx = b.new_context(viewport={"width": 390, "height": 844})
        ctx.add_init_script("try{ localStorage.setItem('jt-mine', JSON.stringify({done:true})); }catch(e){}")   # this test is about the worker, not the first-start sheet
        pg = ctx.new_page()
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

# One clock for the harness and the page: Date.now in the page is shifted to noon Japan time of today's Japan date, so no
# step of a run can straddle midnight and the dates typed into the setup sheet always match the page's "today".
_NOW = datetime.datetime.now(datetime.timezone.utc)
PIN = (_NOW + datetime.timedelta(hours=9)).replace(hour=3, minute=0, second=0, microsecond=0)   # 03:00 UTC = 12:00 JST
CLOCK_SCRIPT = "(function(){ var o = %d; var n = Date.now; Date.now = function(){ return n() + o; }; })();" % int((PIN - _NOW).total_seconds() * 1000)
def jst_date(days=0):
    return (PIN + datetime.timedelta(hours=9, days=days)).strftime("%Y-%m-%d")

def run_setup(pg, errors):
    """The first-start sheet: pick a home zone, dates from yesterday to three days ahead, add a city, save."""
    st = {"shown": pg.locator("#setupSheet").evaluate("e => !e.hidden")}
    if not st["shown"]:
        errors.append("setup sheet did not open on first start"); return st
    pg.select_option("#setupTz", "America/New_York")
    st["dates"] = pg.locator("#setupDatesWrap").evaluate("e => !e.hidden")
    if st["dates"]:
        pg.fill("#setupFrom", jst_date(-1)); pg.fill("#setupTo", jst_date(3))
    pg.fill("#setupNewCity", CITY_A); pg.click("#setupAddCity"); pg.wait_for_timeout(200)
    st["city_options"] = pg.locator("#setupCity option").all_inner_texts()
    pg.fill("#setupNewCity", CITY_C)   # typed but never "added": Save must take it
    st["default_city"] = pg.evaluate("document.querySelector('#setupCity').value")
    want = (trip.get("meta") or {}).get("mainCity")
    if want and st["default_city"] != want: errors.append("suggested city is %r, meta.mainCity says %r" % (st["default_city"], want))
    pg.click("#setupOk"); pg.wait_for_timeout(700)
    st["closed"] = pg.locator("#setupSheet").evaluate("e => e.hidden")
    st["clock"] = pg.locator("#clock").inner_text()
    st["today"] = pg.locator("main").inner_text()[:200].replace("\n", " | ")
    if not st["closed"]: errors.append("setup sheet still open after Save")
    if not re.search(r"\bE[SD]T\b", st["clock"]): errors.append("clock does not show the chosen zone: " + st["clock"])
    if st["dates"] and "day 2 of 5" not in st["today"]: errors.append("Today does not show the phone's own days: " + st["today"])
    if CITY_A not in st["city_options"]: errors.append("added city missing from the city list: " + json.dumps(st["city_options"]))
    return st

CITY_A, CITY_B, CITY_C, CITY_D = "Harness City", "Harness Town", "Harness Village", "Harness Port"   # names no copy's data will have

def own_places(pg, tab, errors, own_days):
    """The phone's own city and place, "+ Day" onto the phone's own day (only while the copy's shared plan has no days),
    and a city added from the Places tab. Every step is guarded so a missing element is reported, not a 30 s hang."""
    o = {}
    pg.set_default_timeout(6000)
    try:
        pg.click(f'[data-city="{CITY_A}"]'); pg.wait_for_timeout(300)
        o["empty_city"] = pg.locator("main").inner_text()[:200].replace("\n", " | ")
        pg.click("[data-addplace]"); pg.wait_for_timeout(300)
        pg.fill("#plName", "Nakatanidou"); pg.fill("#plJa", "中谷堂"); pg.select_option("#plCat", "Food"); pg.fill("#plArea", "Sanjo-dori")
        pg.click("#plOk"); pg.wait_for_timeout(400)
        o["cards"] = pg.locator("main .place").count()
        o["map_link"] = pg.locator("main .place a.btn").first.get_attribute("href") if o["cards"] else None
        if pg.locator("[data-visit]").count():
            pg.locator("[data-visit]").first.click(); pg.wait_for_timeout(300); o["hidden_when_visited"] = pg.locator("main .place").count() == 0
            if pg.locator("#visToggle").count(): pg.click("#visToggle"); pg.wait_for_timeout(300); pg.locator("[data-visit]").first.click(); pg.wait_for_timeout(300)
        o["addday_buttons"] = pg.locator("[data-addday]").count()
        if o["addday_buttons"]:
            pg.locator("[data-addday]").first.click(); pg.wait_for_timeout(300); pg.click("#addOk"); pg.wait_for_timeout(500)
        tab("today"); o["today"] = pg.locator("main").inner_text()[:400].replace("\n", " | ")
        o["remove_buttons"] = pg.locator("[data-rmitem]").count()
        if own_days:   # "Add a city…" from the Today hero's picker sets that day's city
            tab("today"); pg.select_option(".citysel", "__add"); pg.wait_for_timeout(200); pg.fill("#cityName", CITY_D); pg.click("#cityOk"); pg.wait_for_timeout(400)
            o["hero_city"] = pg.locator(".hero .cityrow .city").inner_text()
        tab("places"); pg.click("[data-addcity]"); pg.wait_for_timeout(200); pg.fill("#cityName", CITY_B); pg.click("#cityOk"); pg.wait_for_timeout(400)
        o["cities"] = pg.locator('.filters [data-city]').all_inner_texts()
        pg.click('[data-city="All"]'); pg.wait_for_timeout(300)
    except Exception as e:
        errors.append("own places: " + type(e).__name__ + ": " + str(e).splitlines()[0] + " " + json.dumps(o, ensure_ascii=False)); return o
    finally:
        pg.set_default_timeout(30000)
    if o.get("cards") != 1: errors.append("own place not shown under its city: " + json.dumps(o, ensure_ascii=False))
    if not (o.get("map_link") or "").startswith("https://www.google.com/maps/search/?api=1&query=%E4%B8%AD%E8%B0%B7%E5%A0%82"): errors.append("own place Maps link does not search by its Japanese name: " + str(o.get("map_link")))
    if o.get("hidden_when_visited") is False: errors.append("Visited did not hide the phone's own place")
    if own_days:
        if not o["addday_buttons"]: errors.append("+ Day missing on the phone's own days")
        elif "Nakatanidou" not in o["today"] or not o["remove_buttons"]: errors.append("+ Day did not land on the phone's own day: " + json.dumps(o, ensure_ascii=False))
    elif o["addday_buttons"]: errors.append("+ Day offered on the phone's own place while the shared plan has days (it would write into trip/days)")
    if CITY_B not in o["cities"] or CITY_A not in o["cities"]: errors.append("added cities missing from the filter: " + json.dumps(o["cities"]))
    if CITY_C not in o["cities"]: errors.append("a city typed in the setup sheet but not added was dropped on Save: " + json.dumps(o["cities"]))
    if own_days and o.get("hero_city") != CITY_D: errors.append("Add a city from the Today picker did not set the day's city: " + json.dumps(o.get("hero_city")))
    return o

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
            pg.add_init_script(CLOCK_SCRIPT)
            if MODE == "offline": pg.add_init_script(APK_STUB)
            elif MODE == "live": pg.add_init_script(LIVE_STUB)
            pg.goto("file://" + os.path.abspath(page_path))
            pg.wait_for_timeout(1500)
            out = {"scheme": scheme}
            out["setup"] = run_setup(pg, errors)
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
            if out["setup"].get("shown"): out["own"] = own_places(pg, tab, errors, out["setup"].get("dates"))
            tab("yen"); out["yen"] = pg.locator("main").inner_text()[:160].replace("\n", " | ")
            tab("speak"); pg.wait_for_timeout(600)
            out["speak_themes"] = [t for t in pg.locator("#v-speak button").all_inner_texts() if t.strip()][:40]
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
            if MODE == "live" and out["writes"]: errors.append("the phone's own setup reached the shared database: " + json.dumps(pg.evaluate("window.__writes"))[:400])
            print(json.dumps(out, ensure_ascii=False, indent=1))
            if errors: failed.append(scheme)
            ctx.close()
        b.close()
    if failed:
        print("FAILED:", failed); sys.exit(1)

main()
