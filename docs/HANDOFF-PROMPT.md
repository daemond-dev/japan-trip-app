# First Claude Code session (cloud)

Everything runs from a phone. Once the repo is on GitHub (public, with the files from the zip), open it in Claude Code on the web (claude.ai/code) or the Code tab of the Claude app, make sure the Claude GitHub app has access to the repo, and paste the text below the line as the task. Two things to do on github.com yourself when Claude asks: Settings > Pages > Source: "GitHub Actions", and Settings > Secrets and variables > Actions: the three signing secrets.

---

This repo is a trip-app template handed over from a claude.ai Project; it's meant to be worked on from here with GitHub Actions doing tests, the site and the Android app. Read CLAUDE.md and docs/NOTES.md fully first, then do these in order, and tell me plainly when something can't run in this sandbox instead of retrying.

1. Check that `.github/workflows/test.yml`, `pages.yml` and `apk.yml` are present and parse. Try `pip install -r requirements.txt`, `python -m playwright install chromium` and `python3 scripts/test_page.py live`, `offline`, `static`, `sw`; if the browser can't be downloaded here, skip the local tests and say so.
2. Make the signing key: `python3 scripts/make_keystore.py signing.keystore japantrip <a password you generate> "Japan 2026"`. Give me the base64 line, the alias and the password to put into the repo secrets `SIGNING_KEYSTORE_B64`, `SIGNING_ALIAS` and `SIGNING_PASS`, then delete the file; never commit it.
3. Confirm the "How the owner wants this work done" section in docs/NOTES.md with me and update it. Record there that my own live app stays in its claude.ai Project and this repo is the shareable template, and that the shared copy is the static site plus the APK from Releases.
4. Commit and push to `main` (if you can only open a pull request, do that and I'll merge from my phone). Tell me to set Settings > Pages > Source to "GitHub Actions". After I do, the Pages workflow publishes the site; give me its URL from the workflow run, or tell me where to read it if you can't see the run.
5. Then build the shared dataset for the wedding party: I'll give you the wedding-week schedule and the places. Put them in `data/` without bookings, confirmation numbers, QR codes or anyone's phone number; flag anything that needs advance booking, overpacked days, and conflicts with fixed points (flights, booked hotels, wedding events); keep slack around travel days and the wedding. Push when I say so.
6. When the site is live and the secrets are set, tell me to run Actions > "Build the Android app" with version 1.0 and the site URL as the live URL, and give me the one-line note for the group: iPhone and Android open the link and Add to Home Screen (works offline); Android can also install the APK from the Releases page.
