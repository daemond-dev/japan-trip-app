Build the Android app.

In a cloud sandbox: make sure the owner has the signing secrets (`SIGNING_KEYSTORE_B64`, `SIGNING_ALIAS`, `SIGNING_PASS`; `python3 scripts/make_keystore.py` makes them), then tell them to run Actions > "Build the Android app" > Run workflow with the version and the live URL (the Pages site for a no-account app), or trigger it with `gh workflow run apk.yml -f version=<v> -f live_url=<url>` if `gh` is available. The APK lands on the Releases page. Locally:

1. Read `android/README.md`. Make sure `android/local.properties` exists (copy the example) with `APP_ID`, `APP_NAME`, `LIVE_URL` (the claude.ai artifact, or the GitHub Pages URL for a static-only app) and the keystore settings. Never commit it or the keystore.
2. Install the toolchain if missing (command-line tools, `platforms;android-35`, `build-tools;35.0.0`, ecj, JDK 17+, zip); set `SDK` and `ECJ` for `build.sh`.
3. `cd android && ./build.sh <versionName>`; versionCode defaults to a timestamp so updates install over older builds. The script bundles the current `app/index.html` and `data/` as the offline copy and creates the keystore on the first build.
4. Verify with `aapt2 dump badging` (package, label, version) and `apksigner verify`. Report the APK path and remind the owner to keep the keystore.
