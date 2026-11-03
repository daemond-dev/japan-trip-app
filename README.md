# Japan trip app

A phone app for a trip to Japan, built as a template so anyone in the group can make their own copy. It runs three ways from the same files:

- A static website (GitHub Pages): one link, any phone, no account. Today view that follows the itinerary hour by hour, places with map and transit links and a driver card in Japanese, a yen converter, a phrasebook with spoken audio and full-screen cards to show staff, tips. "Add to Home Screen" installs it with an icon, and it opens offline. Updates whenever the repo is pushed.
- A live claude.ai artifact: the same app with shared data, a to-do list the whole party can tick, booking confirmations and QR codes stored in the app, and an "Ask" chat that answers from the trip data. Needs a Claude plan with artifact capabilities; published from a claude.ai chat (see `docs/SETUP-IN-CLAUDE.md`).
- An Android app wrapping either of those, with an offline copy of the page and data. iPhones use the website or the artifact inside the Claude app.

## Everything runs in the cloud

Nothing needs a computer. Edits happen in Claude Code (on the web, or the Code tab in the Claude mobile app) on this repo; GitHub Actions does the rest on every push: `test.yml` runs the page tests, `pages.yml` publishes the static site, and `apk.yml` (Actions tab, "Run workflow") builds the Android app and puts it on the Releases page.

## Make your own copy

1. GitHub: "Use this template" (a public repo, so the free Pages site works; the template holds no personal data).
2. Settings > Pages > Source: GitHub Actions. Every push to `main` then republishes the site at the URL the workflow prints.
3. Open the repo in Claude Code and type `/setup`. It asks for the surname as written on bookings, home city, who is traveling, allergies, dates and cities, then fills in the data and opens a pull request or pushes. No Claude Code? Edit `data/meta.json` and the "~" phrases in `data/phrases.json` by hand; `data/packs/` has add-on phrase sets (`python3 scripts/add_pack.py kids-and-baby`).
4. Android app: once, add the repo secrets `SIGNING_KEYSTORE_B64`, `SIGNING_ALIAS`, `SIGNING_PASS` (ask Claude Code to run `scripts/make_keystore.py` and give you the values), then run the "Build the Android app" workflow with a version and the site URL. The APK appears under Releases; Android users download it from there.
5. Optional: the live claude.ai copy with Ask, shared to-dos and stored confirmations (`docs/SETUP-IN-CLAUDE.md`).

## What's where

- `app/index.html`: the page. `data/`: the content, one JSON file per document. `data/packs/`: optional phrase packs.
- `android/`: APK source. `scripts/`: build, test and data tools. `docs/NOTES.md`: how it all works, for people and for Claude.
- `CLAUDE.md`: instructions Claude Code reads at the start of every session.

Public note: a GitHub Pages site is readable by anyone with the link, so keep confirmation numbers, QR codes and private phone numbers in the live copy, not in `data/` on a shared repo.
