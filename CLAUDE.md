# Japan trip app

A trip app for a phone: a claude.ai artifact (live: shared data, Ask, to-dos, booking documents), a static website built from the same files (GitHub Pages, no account needed), and an Android wrapper for either. One page (`app/index.html`, vanilla JS, no build step) and data as JSON (`data/`). This repo is a template: copies personalize the data, never the code.

## Read first

- `docs/NOTES.md` before touching `app/index.html`, `android/`, or the data model. It is the full build notes: tabs and features, data model, the three runtime modes, Android bridge, gotchas, loose ends. Keep it current when you change something.
- The "How the owner wants this work done" section of `docs/NOTES.md`. If it hasn't been confirmed with this repo's owner, ask in the first session and update it.

## Layout

- `app/index.html`: the page. Everything personal is in data, not here.
- `data/*.json`: one file per database document (`meta`, `days`, `stays`, `places`, `prices`, `phrases`, `tips`). `data/packs/`: optional phrase packs, merged with `scripts/add_pack.py`. `data/todo/` (optional): one JSON per to-do.
- `android/`: APK source, templated by `android/build.sh` from `android/local.properties` (gitignored; copy the example).
- `scripts/`: `build_static.py` (site, with manifest, icons and the service worker), `make_data.py`, `patch_html.py`, `add_pack.py`, `test_page.py` (Playwright harness, three modes).
- `site/`: generated static site, not committed; `.github/workflows/pages.yml` publishes it on push to `main`.
- `docs/SETUP-IN-CLAUDE.md`: the prompt for publishing a live copy from a claude.ai chat.
- `.claude/commands/`: `/setup`, `/sync`, `/add-booking`, `/build-site`, `/build-apk`.

## Rules

- Test before saying done: `python3 scripts/test_page.py live`, `offline` and `static` (needs Playwright with Chromium; `pip install playwright && python -m playwright install chromium`). Zero console errors in both color schemes is the bar. Fake `Date.now` when checking Today.
- Only change what was asked. If something else looks wrong, say so and ask.
- Validate every JSON file you edit. Data rules: every day ends with a Sleep item and `stay` points at a place id; any item longer than 30 minutes has `end`; `m` is minutes after midnight Japan time or `null`; phrases run most-used first with a "you'll hear" line above its answers; every phrase and sign has `en`, `ja`, `ro`; "~" is the blank in a phrase; romaji is wapuro style (toukyou, arigatou).
- Personalization is data: `meta.title`, `meta.homeTz`, `meta.travelers`, the "~" phrases, the packs. Don't hardcode a name, a city, a time zone or a date in the page.
- Every page change must work in all three modes (live, Android offline, static) or degrade visibly; `docs/NOTES.md` says what each mode has.
- Publishing the artifact and writing its database are claude.ai chat tools, not available here. Keep `app/index.html` and `data/` as the files that chat uses, and when a live copy's page changes in a chat, commit the same page here so they stay identical.
- Never commit `android/local.properties`, a keystore, booking confirmations, QR codes, confirmation numbers, or the couple's or anyone's contact details. The static site is public to anyone with the link.
- Prices in yen with USD in parentheses; times Japan time, 24-hour; the header clock shows home time from `meta.homeTz`.
- Plain prose in replies and notes. No praise openers, no filler.

## Running in a cloud sandbox (Claude Code on the web or in the mobile app)

- Prefer editing and pushing; let GitHub Actions do the heavy work: `test.yml` runs the page tests on every push, `pages.yml` publishes the static site, `apk.yml` builds the Android app (started from the Actions tab with a version and the live URL) and publishes it under Releases.
- Try `pip install -r requirements.txt` and the tests locally; if Chromium can't be downloaded or `dl.google.com` is blocked, say so once and rely on the workflows instead of retrying. Never claim a test passed that didn't run.
- Keystore: `python3 scripts/make_keystore.py signing.keystore <alias> <password> "<name>"` works without Java and prints the base64 for the repo secrets `SIGNING_KEYSTORE_B64`, `SIGNING_ALIAS`, `SIGNING_PASS`. Give the owner those values in the chat and delete the file; don't commit it.
- Work on a branch and open a pull request unless the owner says to push to `main`; merging is what publishes.

## Common commands

```
python3 scripts/build_static.py            # site/ from app/ and data/
python3 scripts/test_page.py static        # or live, offline
python3 scripts/add_pack.py kids-and-baby  # or seafood-allergy; --remove to take out
python3 scripts/test_page.py sw            # static site + service worker on localhost
cd android && ./build.sh 1.0               # APK locally; needs local.properties and the toolchain in android/README.md
                                           # in the cloud: Actions > Build the Android app > Run workflow
```
