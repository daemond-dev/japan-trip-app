package __APP_ID__;   // build.sh replaces this with APP_ID from local.properties

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.res.Configuration;
import android.graphics.Color;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkInfo;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.speech.tts.TextToSpeech;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebChromeClient;
import android.webkit.ValueCallback;
import android.widget.FrameLayout;
import android.widget.Toast;

import org.json.JSONArray;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

/**
 * Japan 2026: shows the live claude.ai app when there's a connection (so it stays in sync),
 * and a bundled offline copy fed by the last data the live app handed over through window.TripApp.
 */
public class MainActivity extends Activity {
    static final String HOST = "japan2026.app";
    static final String OFFLINE = "https://" + HOST + "/index.html";
    static final String LIVE = "__LIVE_URL__";   // build.sh fills this from LIVE_URL in local.properties
    /** The live URL is either the claude.ai artifact (full app: a viewer page with the trip frame pinned inside it) or a
     *  plain website such as the GitHub Pages build (STATIC_LIVE: the page itself, nothing to pin or hide, ready on load). */
    static final boolean STATIC_LIVE = !LIVE.contains("claude.ai");
    static final String LIVE_HOST = Uri.parse(LIVE).getHost();

    WebView web;
    /** Offline copy shown the moment the app opens, while the live page loads underneath; dropped once the trip frame is up. */
    WebView splash;
    FrameLayout root;
    TextToSpeech tts;
    boolean ttsReady = false;
    boolean showingLive = false, liveLoaded = false;
    long lastAutoLive = 0;
    ConnectivityManager.NetworkCallback netCb;
    // The live page sits inside the claude.ai viewer, so it reports whether Back has somewhere to go (wantsBack)
    // and collects Back presses itself (backPending) on a short timer.
    volatile boolean wantsBack = false, backPending = false;
    final Handler ui = new Handler(Looper.getMainLooper());
    // Photo picking for the Ask screen: <input type="file"> needs the app to open a picker and hand back the result.
    static final int REQ_FILE = 4201;
    // Location for the Ask/Places travel times: last fix as JSON for the page, refreshed on request.
    static final int REQ_LOC = 4202;
    volatile String lastFix = "";
    android.location.Location lastLoc = null;
    boolean locAsked = false;
    ValueCallback<Uri[]> fileCb = null;
    Uri camUri = null;
    SharedPreferences prefs;

    boolean isNight() {
        return (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
    }

    @SuppressWarnings("deprecation")
    boolean isOnline() {
        ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (cm == null) return false;
        if (Build.VERSION.SDK_INT >= 23) {
            Network n = cm.getActiveNetwork();
            if (n == null) return false;
            NetworkCapabilities c = cm.getNetworkCapabilities(n);
            return c != null && c.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) && c.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED);
        }
        NetworkInfo i = cm.getActiveNetworkInfo();
        return i != null && i.isConnected();
    }

    File dataFile() { return new File(getFilesDir(), "trip-data.json"); }
    File docsDir() { File f = new File(getFilesDir(), "docs"); f.mkdirs(); return f; }
    static String safeKey(String k) { return k == null ? "" : k.replaceAll("[^A-Za-z0-9_-]", "_"); }
    File pageFile() { return new File(getFilesDir(), "page.html"); }
    /** Hides the claude.ai bar: the trip app's frame is pinned over the whole viewer. Skipped on pages without it (sign-in).
     *  Polls only until the frame is found (then a cheap observer watches for it being replaced), tells the app when the
     *  frame is up (liveReady), and keeps a handle on the trip page's window for passing Back presses in. */
    static final String FIT_JS = "(function(){\n"
        + "  window.__jtFullOff = 0;\n"
        + "  if(window.__jtFull) return; window.__jtFull = 1;\n"
        + "  var pinned = null, timer = 0, ready = false, debounce = 0;\n"
        + "  function fit(){\n"
        + "    if(window.__jtFullOff) return;\n"
        + "    if(pinned && pinned.isConnected && pinned.style.getPropertyValue('position') === 'fixed') return;\n"
        + "    var best = null, area = 0;\n"
        + "    document.querySelectorAll('iframe').forEach(function(f){ var r = f.getBoundingClientRect(), a = r.width * r.height; if(a > area){ area = a; best = f; } });\n"
        + "    if(!best || area < innerWidth * innerHeight * 0.3) return;\n"
        + "    var s = best.style;\n"
        + "    [['position','fixed'],['top','0'],['left','0'],['right','0'],['bottom','0'],['width','100%'],['height','100%'],['max-width','none'],['max-height','none'],['margin','0'],['border','0'],['z-index','2147483647']].forEach(function(p){ s.setProperty(p[0], p[1], 'important'); });\n"
        + "    document.documentElement.style.setProperty('overflow', 'hidden', 'important');\n"
        + "    pinned = best;\n"
        + "    if(timer){ clearInterval(timer); timer = 0; }\n"
        + "    try{ best.contentWindow.postMessage('jt-ping', '*'); }catch(e){}\n"
        + "    if(!ready){ ready = true; try{ TripApp.liveReady(); }catch(e){} }\n"
        + "  }\n"
        + "  fit();\n"
        + "  if(!pinned) timer = setInterval(fit, 700);\n"
        + "  setTimeout(function(){ if(timer){ clearInterval(timer); timer = 0; } }, 90000);\n"
        + "  try{ new MutationObserver(function(){ if(window.__jtFullOff) return; if(pinned && pinned.isConnected) return; clearTimeout(debounce); debounce = setTimeout(fit, 300); }).observe(document.documentElement, {childList:true, subtree:true}); }catch(e){}\n"
        + "  addEventListener('resize', function(){ clearTimeout(debounce); debounce = setTimeout(fit, 200); });\n"
        + "  addEventListener('message', function(e){ if(e.data === 'jt-hello') window.__jtSrc = e.source; });\n"
        + "})();\n";
    static final String UNFIT_JS = "(function(){ window.__jtFullOff = 1; document.querySelectorAll('iframe').forEach(function(f){ ['position','top','left','right','bottom','width','height','max-width','max-height','margin','border','z-index'].forEach(function(p){ f.style.removeProperty(p); }); }); document.documentElement.style.removeProperty('overflow'); })();";
    /** Passes a Back press to the trip page as a window message (the page answers with TripApp.ackBack). */
    static final String BACK_JS = "(function(){ var s = window.__jtSrc || null; if(s){ try{ s.postMessage('jt-back', '*'); }catch(e){} } document.querySelectorAll('iframe').forEach(function(f){ try{ if(f.contentWindow !== s) f.contentWindow.postMessage('jt-back', '*'); }catch(e){} }); })();";
    static final String SHIM = "<script src=\"data.js\"></script><script src=\"shim.js\"></script>";

    static byte[] readAll(InputStream in) throws java.io.IOException {
        java.io.ByteArrayOutputStream o = new java.io.ByteArrayOutputStream();
        byte[] b = new byte[8192]; int n;
        while ((n = in.read(b)) > 0) o.write(b, 0, n);
        in.close();
        return o.toByteArray();
    }

    /** Opens Google Translate directly; links from inside the claude.ai viewer don't reach the app on their own. */
    void launchTranslate(String sl, String tl, String mode) {
        String pkg = "com.google.android.apps.translate";
        sl = sl == null ? "en" : sl.replaceAll("[^a-zA-Z-]", "");
        tl = tl == null ? "ja" : tl.replaceAll("[^a-zA-Z-]", "");
        String web = "https://translate.google.com/?sl=" + sl + "&tl=" + tl + "&op=translate";
        if ("text".equals(mode)) {
            try { Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(web)); i.setPackage(pkg); startActivity(i); return; } catch (Exception e) { }
        }
        Intent li = getPackageManager().getLaunchIntentForPackage(pkg);
        if (li != null) { try { startActivity(li); return; } catch (Exception e) { } }
        openExternal(web);
    }

    /** Status and navigation bar colors follow the light or dark theme; also applied when the theme changes while running. */
    void applyBars() {
        Window w = getWindow();
        boolean night = isNight();
        int surface = night ? Color.rgb(0x18, 0x20, 0x2A) : Color.WHITE;
        if (root != null) root.setBackgroundColor(surface);
        if (web != null) web.setBackgroundColor(surface);
        if (splash != null) splash.setBackgroundColor(surface);
        if (Build.VERSION.SDK_INT >= 30) {
            WindowInsetsController c = w.getInsetsController();
            if (c != null) {
                int light = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
                c.setSystemBarsAppearance(night ? 0 : light, light);
            }
        } else {
            int flags = View.SYSTEM_UI_FLAG_LAYOUT_STABLE | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION;
            if (!night) {
                flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                if (Build.VERSION.SDK_INT >= 26) flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
            }
            w.getDecorView().setSystemUiVisibility(flags);
        }
    }

    @Override
    public void onConfigurationChanged(Configuration c) {
        super.onConfigurationChanged(c);
        applyBars();
    }

    WebView makeWebView(boolean isSplash) {
        WebView v = new WebView(this);
        v.setBackgroundColor(isNight() ? Color.rgb(0x18, 0x20, 0x2A) : Color.WHITE);
        WebSettings s = v.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(true);   // needed for photos picked for the Ask screen (content:// files)
        s.setSupportMultipleWindows(false);
        s.setJavaScriptCanOpenWindowsAutomatically(false);
        s.setTextZoom(100);
        // Look like Chrome rather than an embedded view, so sign-in pages don't refuse it.
        s.setUserAgentString(s.getUserAgentString().replace("; wv", "").replace(" Version/4.0", ""));
        v.addJavascriptInterface(new Speech(), "NihongoApp");
        v.addJavascriptInterface(new Trip(isSplash), "TripApp");
        v.setWebViewClient(new Client(isSplash));
        v.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView w, ValueCallback<Uri[]> cb, FileChooserParams params) {
                if (fileCb != null) fileCb.onReceiveValue(null);
                fileCb = cb;
                Intent pick = new Intent(Intent.ACTION_GET_CONTENT);
                pick.addCategory(Intent.CATEGORY_OPENABLE);
                pick.setType("image/*");
                pick.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE);
                Intent chooser = Intent.createChooser(pick, "Add a photo");
                camUri = null;
                if (Build.VERSION.SDK_INT >= 29) {
                    // The camera writes straight into Pictures/Japan 2026, so no file-sharing setup is needed.
                    try {
                        android.content.ContentValues cv = new android.content.ContentValues();
                        cv.put(android.provider.MediaStore.MediaColumns.DISPLAY_NAME, "Japan2026_" + System.currentTimeMillis() + ".jpg");
                        cv.put(android.provider.MediaStore.MediaColumns.MIME_TYPE, "image/jpeg");
                        cv.put(android.provider.MediaStore.MediaColumns.RELATIVE_PATH, android.os.Environment.DIRECTORY_PICTURES + "/Japan 2026");
                        camUri = getContentResolver().insert(android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI, cv);
                        if (camUri != null) {
                            Intent cam = new Intent(android.provider.MediaStore.ACTION_IMAGE_CAPTURE);
                            cam.putExtra(android.provider.MediaStore.EXTRA_OUTPUT, camUri);
                            cam.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);
                            chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[]{cam});
                        }
                    } catch (Exception e) { camUri = null; }
                }
                try { startActivityForResult(chooser, REQ_FILE); }
                catch (Exception e) { dropCam(); fileCb = null; return false; }
                return true;
            }
        });
        return v;
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("trip", MODE_PRIVATE);
        Window w = getWindow();
        w.setStatusBarColor(Color.TRANSPARENT);
        w.setNavigationBarColor(Color.TRANSPARENT);
        if (Build.VERSION.SDK_INT >= 30) w.setDecorFitsSystemWindows(false);

        root = new FrameLayout(this);
        root.setOnApplyWindowInsetsListener((v, ins) -> {
            int l, t, r, b;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets s = ins.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime() | WindowInsets.Type.displayCutout());
                l = s.left; t = s.top; r = s.right; b = s.bottom;
            } else {
                l = ins.getSystemWindowInsetLeft(); t = ins.getSystemWindowInsetTop();
                r = ins.getSystemWindowInsetRight(); b = ins.getSystemWindowInsetBottom();
            }
            v.setPadding(l, t, r, b);
            return Build.VERSION.SDK_INT >= 30 ? WindowInsets.CONSUMED : ins.consumeSystemWindowInsets();
        });

        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);
        web = makeWebView(false);
        cm.setAcceptThirdPartyCookies(web, true);
        root.addView(web, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        setContentView(root);
        applyBars();
        root.requestApplyInsets();

        tts = new TextToSpeech(this, status -> {
            if (status == TextToSpeech.SUCCESS) {
                int r = tts.setLanguage(Locale.JAPAN);
                ttsReady = r != TextToSpeech.LANG_MISSING_DATA && r != TextToSpeech.LANG_NOT_SUPPORTED;
            }
        });

        if (isOnline()) {
            // The saved copy opens at once; the live page takes over as soon as its frame is up (same tab and scroll spot).
            if (dataFile().exists()) showSplash();
            loadLive();
        } else loadOffline(null);
    }

    void showSplash() {
        if (splash != null) return;
        splash = makeWebView(true);
        root.addView(splash, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        splash.loadUrl(OFFLINE);
    }

    void dropSplash() {
        if (splash == null) return;
        WebView v = splash; splash = null;
        root.removeView(v);
        v.destroy();
    }

    void loadLive() {
        wantsBack = false; backPending = false;
        showingLive = true;
        liveLoaded = false;
        web.loadUrl(LIVE);
        // If the live page hasn't even started showing after 25 seconds and we have a saved copy, fall back to it.
        ui.postDelayed(() -> {
            if (showingLive && !liveLoaded && dataFile().exists()) loadOffline("Slow connection, so this is the copy saved on this phone. Pull down to try the live app again.");
        }, 25000);
    }

    void loadOffline(String msg) {
        wantsBack = false; backPending = false;
        showingLive = false;
        dropSplash();
        web.loadUrl(OFFLINE);
        if (msg != null) toast(msg);
    }

    /** Opened offline, and a working connection has since appeared: switch to the live app (at most once a minute). */
    void netBack() {
        if (showingLive || isFinishing()) return;
        long now = SystemClock.elapsedRealtime();
        if (now - lastAutoLive < 60000) return;
        lastAutoLive = now;
        toast("Connection is back, opening the live app.");
        loadLive();
    }

    void watchNetwork(boolean on) {
        ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (cm == null) return;
        if (on) {
            if (netCb != null) return;
            netCb = new ConnectivityManager.NetworkCallback() {
                @Override
                public void onCapabilitiesChanged(Network n, NetworkCapabilities c) {
                    if (c.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) && c.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)) ui.post(MainActivity.this::netBack);
                }
            };
            try { cm.registerDefaultNetworkCallback(netCb); } catch (Exception e) { netCb = null; }
        } else if (netCb != null) {
            try { cm.unregisterNetworkCallback(netCb); } catch (Exception e) { }
            netCb = null;
        }
    }

    void clearBright() {
        android.view.WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.screenBrightness = android.view.WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE;
        getWindow().setAttributes(lp);
        getWindow().clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }

    @Override
    protected void onPause() {
        super.onPause();
        watchNetwork(false);
        clearBright();
        web.onPause();
        if (splash != null) splash.onPause();
        CookieManager.getInstance().flush();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
        if (splash != null) splash.onResume();
        watchNetwork(true);
    }

    @Override
    protected void onDestroy() {
        if (tts != null) { tts.stop(); tts.shutdown(); }
        watchNetwork(false);
        dropSplash();
        web.destroy();
        super.onDestroy();
    }

    boolean hasLocPerm() {
        return checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION) == android.content.pm.PackageManager.PERMISSION_GRANTED
            || checkSelfPermission(android.Manifest.permission.ACCESS_COARSE_LOCATION) == android.content.pm.PackageManager.PERMISSION_GRANTED;
    }
    void keepLoc(android.location.Location l) {
        if (l == null) return;
        if (lastLoc != null && l.getTime() < lastLoc.getTime()) return;
        lastLoc = l;
        lastFix = "{\"lat\":" + l.getLatitude() + ",\"lng\":" + l.getLongitude() + ",\"acc\":" + Math.round(l.getAccuracy()) + ",\"ts\":" + l.getTime() + "}";
    }
    void requestFix() {
        ui.post(() -> {
            if (!hasLocPerm()) {
                if (!locAsked) { locAsked = true; requestPermissions(new String[]{android.Manifest.permission.ACCESS_FINE_LOCATION, android.Manifest.permission.ACCESS_COARSE_LOCATION}, REQ_LOC); }
                return;
            }
            startFix();
        });
    }
    @SuppressWarnings({"MissingPermission", "deprecation"})
    void startFix() {
        try {
            android.location.LocationManager lm = (android.location.LocationManager) getSystemService(LOCATION_SERVICE);
            for (String prov : new String[]{"fused", android.location.LocationManager.GPS_PROVIDER, android.location.LocationManager.NETWORK_PROVIDER}) {
                try { keepLoc(lm.getLastKnownLocation(prov)); } catch (Exception e) { }
            }
            String prov = lm.isProviderEnabled(android.location.LocationManager.GPS_PROVIDER) ? android.location.LocationManager.GPS_PROVIDER : android.location.LocationManager.NETWORK_PROVIDER;
            if (Build.VERSION.SDK_INT >= 30) lm.getCurrentLocation(prov, null, getMainExecutor(), this::keepLoc);
            else lm.requestSingleUpdate(prov, new android.location.LocationListener() {
                @Override public void onLocationChanged(android.location.Location l) { keepLoc(l); }
                @Override public void onStatusChanged(String p, int st, Bundle b) { }
                @Override public void onProviderEnabled(String p) { }
                @Override public void onProviderDisabled(String p) { }
            }, Looper.getMainLooper());
        } catch (Exception e) { }
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] results) {
        super.onRequestPermissionsResult(req, perms, results);
        if (req == REQ_LOC && hasLocPerm()) startFix();
    }

    void dropCam() { if (camUri != null) { try { getContentResolver().delete(camUri, null, null); } catch (Exception e) { } camUri = null; } }

    @Override
    protected void onActivityResult(int req, int result, Intent data) {
        super.onActivityResult(req, result, data);
        if (req != REQ_FILE || fileCb == null) return;
        Uri[] out = null;
        if (result == RESULT_OK) {
            if (data != null && data.getClipData() != null) {
                ClipData c = data.getClipData(); out = new Uri[c.getItemCount()];
                for (int i = 0; i < out.length; i++) out[i] = c.getItemAt(i).getUri();
                dropCam();
            } else if (data != null && data.getData() != null) {
                out = new Uri[]{data.getData()}; dropCam();
            } else if (camUri != null) {
                out = new Uri[]{camUri}; camUri = null;   // photo taken with the camera
            }
        } else dropCam();
        fileCb.onReceiveValue(out);
        fileCb = null;
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (splash != null) {
            splash.evaluateJavascript("window.__apkBack ? String(window.__apkBack()) : 'false'", v -> {
                if (!"\"true\"".equals(v)) finish();
            });
            return;
        }
        if (showingLive) {
            if (wantsBack) {
                backPending = true;
                web.evaluateJavascript(BACK_JS, null);
                // If the page doesn't answer (busy or stuck), fall back to normal Back.
                ui.postDelayed(() -> { if (backPending) { backPending = false; if (web.canGoBack()) web.goBack(); else finish(); } }, 700);
                return;
            }
            if (web.canGoBack()) web.goBack(); else finish();
            return;
        }
        web.evaluateJavascript("window.__apkBack ? String(window.__apkBack()) : 'false'", v -> {
            if (!"\"true\"".equals(v)) finish();
        });
    }

    static boolean inApp(String host) {
        if (host == null) return false;
        return host.equals(HOST) || host.equals(LIVE_HOST) || host.equals("claude.ai") || host.endsWith(".claude.ai")
            || host.equals("anthropic.com") || host.endsWith(".anthropic.com")
            || host.endsWith("claudeusercontent.com")
            || host.startsWith("accounts.google.") || host.equals("accounts.youtube.com");
    }

    boolean openExternal(String url) {
        try {
            if (url.startsWith("intent:")) {
                Intent i = Intent.parseUri(url, Intent.URI_INTENT_SCHEME);
                i.setComponent(null);
                i.setSelector(null);
                try {
                    startActivity(i);
                } catch (ActivityNotFoundException e) {
                    String fb = i.getStringExtra("browser_fallback_url");
                    if (fb != null) startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(fb)));
                    else toast("That app isn't installed.");
                }
                return true;
            }
            Intent i;
            if (url.startsWith("tel:")) i = new Intent(Intent.ACTION_DIAL, Uri.parse(url));
            else if (url.startsWith("mailto:")) i = new Intent(Intent.ACTION_SENDTO, Uri.parse(url));
            else i = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
            startActivity(i);
        } catch (Exception e) {
            toast("No app on this phone can open that link.");
        }
        return true;
    }

    void toast(String m) { runOnUiThread(() -> Toast.makeText(this, m, Toast.LENGTH_LONG).show()); }

    class Client extends WebViewClient {
        final boolean isSplash;
        Client(boolean isSplash) { this.isSplash = isSplash; }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
            Uri u = req.getUrl();
            String scheme = u.getScheme() == null ? "" : u.getScheme();
            boolean web = scheme.equals("http") || scheme.equals("https");
            if (web && !req.isForMainFrame()) return false;
            if (web && inApp(u.getHost())) {
                if (isSplash) return !HOST.equals(u.getHost());   // the splash only ever shows the offline copy
                if (HOST.equals(u.getHost())) showingLive = false;
                else if (!showingLive) { showingLive = true; liveLoaded = false; }
                return false;
            }
            return openExternal(u.toString());
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (isSplash) return;
            if (showingLive && url != null && !url.contains(HOST)) {
                liveLoaded = true;
                if (STATIC_LIVE) { dropSplash(); return; }   // a plain site is the page itself: nothing to pin, ready now
                if (!prefs.getBoolean("chrome", false)) view.evaluateJavascript(FIT_JS, null);
                else dropSplash();
                // A page without the trip frame (sign-in) must still be reachable: drop the splash after a while regardless.
                ui.postDelayed(MainActivity.this::dropSplash, 20000);
            }
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
            if (isSplash) return;
            if (req.isForMainFrame() && showingLive) loadOffline("No connection, so this is the copy saved on this phone.");
        }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
            Uri u = req.getUrl();
            if (!HOST.equals(u.getHost())) return null;
            String path = u.getPath();
            if (path == null || path.equals("/") || path.isEmpty()) path = "/index.html";
            // Offline page: the newest page code the live app handed over, else the copy built into the APK.
            if (path.equals("/index.html") && pageFile().exists()) {
                try {
                    String html = new String(readAll(new FileInputStream(pageFile())), StandardCharsets.UTF_8);
                    int at = html.indexOf("<head>");
                    if (at >= 0 && html.contains("function renderToday")) {
                        html = html.substring(0, at + 6) + SHIM + html.substring(at + 6);
                        Map<String, String> h = new HashMap<>();
                        h.put("Cache-Control", "no-cache");
                        return new WebResourceResponse("text/html", "utf-8", 200, "OK", h, new ByteArrayInputStream(html.getBytes(StandardCharsets.UTF_8)));
                    }
                } catch (Exception e) { }
            }
            try {
                InputStream in = getAssets().open("www" + path);
                Map<String, String> h = new HashMap<>();
                h.put("Cache-Control", "no-cache");
                return new WebResourceResponse(mime(path), "utf-8", 200, "OK", h, in);
            } catch (Exception e) {
                return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", new HashMap<>(), new ByteArrayInputStream(new byte[0]));
            }
        }
    }

    static String mime(String p) {
        if (p.endsWith(".html")) return "text/html";
        if (p.endsWith(".js")) return "application/javascript";
        if (p.endsWith(".json")) return "application/json";
        if (p.endsWith(".css")) return "text/css";
        if (p.endsWith(".png")) return "image/png";
        return "application/octet-stream";
    }

    class Speech {
        @JavascriptInterface
        public void speak(String text, double rate) {
            if (tts == null) return;
            if (!ttsReady) { toast("Japanese voice isn't installed. Add it in Settings, Text-to-speech."); return; }
            tts.setSpeechRate((float) (rate > 0 ? rate : 0.9));
            tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, "jt");
        }

        @JavascriptInterface
        public void stopSpeech() { if (tts != null) tts.stop(); }

        @JavascriptInterface
        public String version() { return "1.17"; }
    }

    /** Data hand-off between the live page and the offline copy. */
    class Trip {
        final boolean onSplash;   // the splash WebView only ever shows the offline copy
        Trip(boolean onSplash) { this.onSplash = onSplash; }
        /** True when this page is the live URL and that URL is a plain website (GitHub Pages), not claude.ai: shim.js
         *  then treats the page as the shared static copy and hands its fresh data to the app for offline use. */
        @JavascriptInterface
        public boolean isStaticHost() { return STATIC_LIVE && !onSplash && showingLive; }
        @JavascriptInterface
        public synchronized void saveData(String json) {
            try (FileOutputStream o = new FileOutputStream(dataFile())) { o.write(json.getBytes(StandardCharsets.UTF_8)); } catch (Exception e) { }
        }

        @JavascriptInterface
        public synchronized String getData() {
            File f = dataFile();
            if (!f.exists()) return "";
            try (FileInputStream in = new FileInputStream(f)) {
                byte[] b = new byte[(int) f.length()];
                int n = 0; while (n < b.length) { int r = in.read(b, n, b.length - n); if (r < 0) break; n += r; }
                return new String(b, 0, n, StandardCharsets.UTF_8);
            } catch (Exception e) { return ""; }
        }

        @JavascriptInterface
        public String getLocal(String k) { return prefs.getString("l:" + k, ""); }

        @JavascriptInterface
        public void saveLocal(String k, String v) { prefs.edit().putString("l:" + k, v == null ? "" : v).apply(); }

        @JavascriptInterface
        public synchronized void addPending(String json) {
            try {
                JSONArray a = new JSONArray(prefs.getString("pending", "[]"));
                a.put(new org.json.JSONObject(json));
                prefs.edit().putString("pending", a.toString()).apply();
            } catch (Exception e) { }
        }

        @JavascriptInterface
        public synchronized String takePending() {
            String p = prefs.getString("pending", "[]");
            prefs.edit().putString("pending", "[]").apply();
            return p;
        }

        @JavascriptInterface
        public boolean isOnline() { return MainActivity.this.isOnline(); }

        @JavascriptInterface
        public void goLive() { ui.post(MainActivity.this::loadLive); }

        @JavascriptInterface
        public boolean inApp() { return true; }

        @JavascriptInterface
        public void setWantsBack(boolean v) { wantsBack = v; }

        /** Settings switch: show or hide the claude.ai bar above the app. */
        @JavascriptInterface
        public void setChrome(boolean show) {
            if (STATIC_LIVE) return;   // nothing to hide on a plain site
            prefs.edit().putBoolean("chrome", show).apply();
            ui.post(() -> web.evaluateJavascript(show ? UNFIT_JS : FIT_JS, null));
        }

        @JavascriptInterface
        public boolean chromeShown() { return prefs.getBoolean("chrome", false); }

        /** Older page versions polled for Back presses; kept so they still work. */
        @JavascriptInterface
        public boolean takeBack() { boolean b = backPending; backPending = false; return b; }

        /** The page handled a Back press that was passed in with BACK_JS. */
        @JavascriptInterface
        public void ackBack() { backPending = false; }

        /** The claude.ai page has the trip frame up: drop the offline copy shown while it loaded. */
        @JavascriptInterface
        public void liveReady() { ui.post(MainActivity.this::dropSplash); }

        /** "online", "captive" (Wi-Fi portal not signed in), "noinet" (connected, no internet yet) or "none". */
        @JavascriptInterface
        public String netState() {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null || Build.VERSION.SDK_INT < 23) return MainActivity.this.isOnline() ? "online" : "none";
            Network n = cm.getActiveNetwork();
            if (n == null) return "none";
            NetworkCapabilities c = cm.getNetworkCapabilities(n);
            if (c == null) return "none";
            if (c.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)) return "online";
            if (c.hasCapability(NetworkCapabilities.NET_CAPABILITY_CAPTIVE_PORTAL)) return "captive";
            return "noinet";
        }

        @JavascriptInterface
        public void copy(String text) {
            ui.post(() -> { try { ClipboardManager cb = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE); cb.setPrimaryClip(ClipData.newPlainText("Japan 2026", text == null ? "" : text)); } catch (Exception e) { } });
        }

        /** Last location fix as JSON {lat,lng,acc,ts}, or "" when none yet (or permission not given). */
        @JavascriptInterface
        public String getLocation() { return lastFix; }

        /** Asks for location permission the first time, then refreshes the fix; the page polls getLocation(). */
        @JavascriptInterface
        public void requestLocation() { requestFix(); }

        /** Reads the clipboard (kept for older pages; the Ask screen no longer has a Paste button). */
        @JavascriptInterface
        public String paste() {
            final String[] out = {""};
            final java.util.concurrent.CountDownLatch done = new java.util.concurrent.CountDownLatch(1);
            ui.post(() -> {
                try {
                    ClipboardManager cb = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                    ClipData c = cb.getPrimaryClip();
                    if (c != null && c.getItemCount() > 0) { CharSequence t = c.getItemAt(0).coerceToText(MainActivity.this); out[0] = t == null ? "" : t.toString(); }
                } catch (Exception e) { }
                done.countDown();
            });
            try { done.await(1500, java.util.concurrent.TimeUnit.MILLISECONDS); } catch (InterruptedException e) { }
            return out[0];
        }

        /** Saves a file the page made (base64), such as a booking document as a PDF, to Downloads/Japan 2026, then opens it. */
        @JavascriptInterface
        public boolean saveFile(String name, String b64) {
            try {
                byte[] data = android.util.Base64.decode(b64, android.util.Base64.DEFAULT);
                String safe = name == null ? "" : name.replaceAll("[\\\\/:*?\"<>|]", " ").replaceAll("\\s+", " ").trim();
                if (safe.isEmpty()) safe = "document.pdf";
                final String mime = safe.toLowerCase().endsWith(".pdf") ? "application/pdf" : "application/octet-stream";
                Uri uri = null;
                if (Build.VERSION.SDK_INT >= 29) {
                    android.content.ContentValues cv = new android.content.ContentValues();
                    cv.put(android.provider.MediaStore.MediaColumns.DISPLAY_NAME, safe);
                    cv.put(android.provider.MediaStore.MediaColumns.MIME_TYPE, mime);
                    cv.put(android.provider.MediaStore.MediaColumns.RELATIVE_PATH, android.os.Environment.DIRECTORY_DOWNLOADS + "/Japan 2026");
                    uri = getContentResolver().insert(android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, cv);
                    if (uri == null) return false;
                    try (java.io.OutputStream o = getContentResolver().openOutputStream(uri)) { o.write(data); }
                } else {
                    File dir = getExternalFilesDir(android.os.Environment.DIRECTORY_DOWNLOADS);
                    if (dir == null) return false;
                    dir.mkdirs();
                    try (FileOutputStream o = new FileOutputStream(new File(dir, safe))) { o.write(data); }
                }
                final Uri u = uri;
                if (u != null) ui.post(() -> {
                    try { Intent v = new Intent(Intent.ACTION_VIEW); v.setDataAndType(u, mime); v.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION); startActivity(v); }
                    catch (Exception e) { }
                });
                return true;
            } catch (Exception e) { return false; }
        }

        /** Booking pages (data URLs) kept in the app's files so the offline copy can show them. */
        @JavascriptInterface
        public void saveDoc(String key, String dataUrl) {
            if (dataUrl == null || dataUrl.length() < 16) return;
            try (FileOutputStream o = new FileOutputStream(new File(docsDir(), safeKey(key)))) { o.write(dataUrl.getBytes(StandardCharsets.UTF_8)); } catch (Exception e) { }
        }

        @JavascriptInterface
        public String getDoc(String key) {
            File f = new File(docsDir(), safeKey(key));
            if (!f.exists()) return "";
            try (FileInputStream in = new FileInputStream(f)) { return new String(readAll(in), StandardCharsets.UTF_8); } catch (Exception e) { return ""; }
        }

        @JavascriptInterface
        public int docCount() { String[] l = docsDir().list(); return l == null ? 0 : l.length; }

        @JavascriptInterface
        public String pageVersion() { return prefs.getString("pageVersion", ""); }

        /** Stores the live page's code so the offline copy stays current without a new APK. */
        @JavascriptInterface
        public synchronized void savePage(String version, String html) {
            if (html == null || !html.contains("function renderToday") || !html.contains("<head>")) return;
            try (FileOutputStream o = new FileOutputStream(pageFile())) {
                o.write(html.getBytes(StandardCharsets.UTF_8));
                prefs.edit().putString("pageVersion", version == null ? "" : version).apply();
            } catch (Exception e) { }
        }

        @JavascriptInterface
        public void openTranslate(String sl, String tl, String mode) { ui.post(() -> launchTranslate(sl, tl, mode)); }

        /** Full brightness and screen kept on while a QR code is showing, so scanners read it. */
        @JavascriptInterface
        public void setBright(boolean on) {
            ui.post(() -> {
                android.view.WindowManager.LayoutParams lp = getWindow().getAttributes();
                lp.screenBrightness = on ? 1f : android.view.WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE;
                getWindow().setAttributes(lp);
                if (on) getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                else getWindow().clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            });
        }
    }
}
