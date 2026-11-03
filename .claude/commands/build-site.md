Build and check the static site.

1. `python3 scripts/build_static.py`, then `python3 scripts/test_page.py static` and `sw` (and `live` if the page changed). Zero console errors in both color schemes. If Playwright can't install here, say so and rely on `test.yml` after the push.
2. Check `git status`: `site/` is generated and must not be committed; `data/` must not contain confirmation numbers, QR codes or private contact details if the repo is shared.
3. Commit and push to `main`; the Pages workflow publishes. The first time, the owner sets Settings > Pages > Source to "GitHub Actions" in the GitHub repo. Give them the site URL from the workflow run, and the install note: open it in Safari or Chrome, "Add to Home Screen", and it works offline from then on.
