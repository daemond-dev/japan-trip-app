#!/usr/bin/env python3
"""Bundle the app data into a data.js for the offline copy and the static site.
Usage: python3 scripts/make_data.py <data dir> <output data.js>
<data dir> holds one JSON file per trip document (meta, days, stays, places, prices, phrases, tips, and the optional
pastMarks, visited, phrasePins, phraseThemes, phraseOrder) and, optionally, a todo/ folder with one JSON file per item.
A packs/ folder inside it is ignored (see scripts/add_pack.py). Works on a folder from Artifact read_db too, when its
trip/ subfolder is passed."""
import json, glob, os, sys, datetime
base, out = sys.argv[1], sys.argv[2]
trip = {os.path.basename(f)[:-5]: json.load(open(f, encoding="utf-8")) for f in sorted(glob.glob(base + "/*.json"))}
todo = []
for f in sorted(glob.glob(base + "/todo/*.json")):
    d = json.load(open(f, encoding="utf-8")); d["id"] = os.path.basename(f)[:-5]; todo.append(d)
now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)
built = now.strftime("%b ") + str(now.day) + now.strftime(", %H:%M") + " Japan time"
os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
open(out, "w", encoding="utf-8").write("window.__TRIP_DATA = " + json.dumps({"built": built, "trip": trip, "todo": todo}, ensure_ascii=False) + ";\n")
print(built, sorted(trip), len(todo))
