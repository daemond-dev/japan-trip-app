#!/usr/bin/env python3
"""Merge an optional phrase pack from data/packs/ into data/phrases.json and data/tips.json, or remove it.
Usage: python3 scripts/add_pack.py <pack name> [--remove]
A pack is {name, description, sections:[{after, name, phrases}], signs, synonyms, tips, optional_phrases?, travelers_note?}.
Adding is idempotent: a section, sign, synonym group or tip group that is already present is skipped.
optional_phrases are NOT added; add the right one by hand once the owner has answered the pack's question."""
import json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
name = sys.argv[1]; remove = "--remove" in sys.argv
pack = json.load(open(os.path.join(ROOT, "data", "packs", name + ".json"), encoding="utf-8"))
pp, tp = os.path.join(ROOT, "data", "phrases.json"), os.path.join(ROOT, "data", "tips.json")
P = json.load(open(pp, encoding="utf-8")); T = json.load(open(tp, encoding="utf-8"))
sec_names = {s["name"] for s in pack["sections"]}; sign_ja = {s["ja"] for s in pack.get("signs", [])}
tip_names = {g["name"] for g in pack.get("tips", [])}
if remove:
    P["groups"] = [g for g in P["groups"] if g["name"] not in sec_names]
    P["signs"] = [s for s in P["signs"] if s["ja"] not in sign_ja]
    P["synonyms"] = [g for g in P["synonyms"] if g not in pack.get("synonyms", [])]
    T["groups"] = [g for g in T["groups"] if g["name"] not in tip_names]
else:
    have = {g["name"] for g in P["groups"]}
    for sec in pack["sections"]:
        if sec["name"] in have: continue
        idx = next((i for i, g in enumerate(P["groups"]) if g["name"] == sec.get("after")), len(P["groups"]) - 1)
        P["groups"].insert(idx + 1, {"name": sec["name"], "phrases": sec["phrases"]})
    have_signs = {s["ja"] for s in P["signs"]}
    P["signs"] += [s for s in pack.get("signs", []) if s["ja"] not in have_signs]
    P["synonyms"] += [g for g in pack.get("synonyms", []) if g not in P["synonyms"]]
    have_tips = {g["name"] for g in T["groups"]}
    T["groups"] += [g for g in pack.get("tips", []) if g["name"] not in have_tips]
for path, doc in ((pp, P), (tp, T)):
    with open(path, "w", encoding="utf-8") as f: json.dump(doc, f, ensure_ascii=False, indent=1)
print(("removed" if remove else "added"), name, "| sections:", [g["name"] for g in P["groups"]])
if not remove and pack.get("optional_phrases"): print("optional phrases not added (pick one after asking):", json.dumps(pack["optional_phrases"], ensure_ascii=False))
if not remove and pack.get("travelers_note"): print("add to meta.travelers:", pack["travelers_note"])
