# Android app

A plain Java WebView app (no Gradle) around the trip page. Settings come from `local.properties` (copy `local.properties.example`): package name, launcher label, the live URL, and the signing key. `build.sh` templates `AndroidManifest.xml`, `src/MainActivity.java` and `res/values/strings.xml` from them, generates `assets/www/index.html` and `assets/www/data.js` from `../app` and `../data`, and creates the keystore on the first build if it is missing. Keep the keystore and never commit it: updates must be signed with the same key or the phone refuses them.

The live URL is the published claude.ai artifact (full app: live sync, Ask, shared to-dos) or the GitHub Pages site (static copy that updates when the site is republished; the app keeps the latest data for offline use and shows its bundled page when there is no connection). With a Pages URL the app needs no claude.ai account.

- Online: opens the live artifact in the WebView, so it is always in sync. While that loads, the saved offline copy shows in a second WebView on top (same tab and scroll spot through the shared settings store); the claude.ai page calls `TripApp.liveReady()` once the trip frame is pinned and the copy is dropped (or after 20 s on a page without the frame, such as sign-in). The live page hands its data, settings (theme, own yen rate, past marks, filters, last spot) and booking pages (`saveDoc`/`getDoc`, files under `files/docs/`) to the app through `window.TripApp`, and applies to-do ticks queued offline (`takePending`).
- Offline (no connection, page error, or nothing loaded after 25 s with a saved copy): opens the offline page fed by the last saved data (`getData`), falling back to `data.js`. Sync now or pull-down goes live again when there's a connection (`goLive`); a network callback also switches to live on its own when a validated connection appears (at most once a minute). `netState()` tells the page about captive-portal Wi-Fi.
- The offline page updates itself: the live page reads the db doc `app/page` ({version, html}) and hands it over with `savePage`; the app then serves that html with `data.js` and `shim.js` injected after the first `<head>`. The copy in `assets/www/index.html` is only the fallback. Rebuild the APK only when the Java code or shim.js changes.
- Back button, live: the page calls `TripApp.setWantsBack(bool)` only when the answer changes; on Back the app runs `BACK_JS`, which posts `jt-back` into the trip page's window (`window.__jtSrc`, learned from the page's `jt-hello` message, or every iframe), and the page answers with `TripApp.ackBack()`. No answer within 700 ms means normal Back. `takeBack()` remains for older page versions that polled. Offline: the app calls `window.__apkBack` directly.
- Translate: links don't launch apps from inside the claude.ai viewer, so the page calls `TripApp.openTranslate(sl, tl, mode)` and the app opens Google Translate itself.
- Copy: `TripApp.copy(text)` puts text on the clipboard (the viewer may not allow the web clipboard).
- `FIT_JS` pins the trip frame over the claude.ai bar; it polls only until the frame is found, then a debounced MutationObserver watches for the frame being replaced. Settings > Android app > "Show the claude.ai bar" undoes it (`setChrome`).
- QR codes: `setBright(true)` sets full brightness and keeps the screen on; the page releases it on close or after three minutes, and the app clears it on pause.
- Sign in to claude.ai once inside the app. The user agent drops the WebView marker so Google sign-in pages don't refuse it.

- Package: `APP_ID` from local.properties. Plain Java WebView, no Gradle. The offline page loads from assets through a fake host (`https://japan2026.app/`).
- `assets/www/index.html` is the live page run through `patch_html.py` (it adds `data.js` and `shim.js` after `<head>`; the page itself handles `window.__APK`); `data.js` is the database bundled by `make_data.py` (only used before the first live sync); `shim.js` stands in for the claude.ai runtime offline and expires to-do overrides older than the saved data.
- Links open in other apps: Google Maps, Translate (intent links), phone, email, browser.
- Speech uses Android text-to-speech through `window.NihongoApp.speak`, the hook the page already has.
- Always sign with the same keystore (`KEYSTORE`, `KS_ALIAS`, `KS_PASS` in local.properties). A different key means the phone refuses the update and the app must be uninstalled first, which wipes its saved ticks.

Rebuild only when the app code or shim.js changes (plan and page changes sync on their own when the live URL is a claude.ai artifact; with a Pages URL, page changes arrive with the next publish of the site):
1. Make sure `../app/index.html` and `../data/` are current (they become the bundled offline copy).
2. `SDK=/opt/android/sdk ECJ=/tmp/ecj.jar ./build.sh <versionName> [versionCode]` from this folder. versionCode must go up on every build that will be installed over an older one; the script defaults it to a timestamp.

Toolchain (about five minutes on a fresh machine): Android command-line tools from dl.google.com (`commandlinetools-linux-11076708_latest.zip`), `sdkmanager --licenses`, then `sdkmanager "platforms;android-35" "build-tools;35.0.0"`; ecj 3.38.0 from Maven Central (`org/eclipse/jdt/ecj/3.38.0/ecj-3.38.0.jar`); OpenJDK 17 or 21; `zip`; python3.
