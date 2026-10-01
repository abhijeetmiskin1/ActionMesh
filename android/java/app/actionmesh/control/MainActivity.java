package app.actionmesh.control;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.Menu;
import android.view.MenuItem;
import android.view.ViewGroup;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.Toast;

/**
 * A thin native shell around the ActionMesh control room.
 *
 * <p>The application is a permission-aware execution platform whose interface is
 * already served at {@code /dashboard} by the ActionMesh server, and is already
 * installable as a PWA. This APK exists so the control room can be a real
 * home-screen app on Android without depending on browser "Add to home screen".
 *
 * <p>It deliberately holds <em>no</em> application logic: it renders whatever the
 * trusted server sends and nothing else. That keeps a single source of truth for
 * policy, execution and audit, and means this shell can never diverge from the
 * server's behaviour. There is no AndroidX/AppCompat dependency, so the APK is
 * built from the Android SDK build-tools directly (see scripts/build_apk.py).
 */
public class MainActivity extends Activity {

    private static final String PREFS = "actionmesh";
    private static final String KEY_URL = "server_url";
    private static final String COLOR_DEEP = "#153b37";

    private WebView web;
    private SharedPreferences prefs;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor(COLOR_DEEP));

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor(COLOR_DEEP));

        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);

        web.setWebViewClient(new ShellClient());
        web.setWebChromeClient(new WebChromeClient());

        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        if (state != null) {
            web.restoreState(state);
        } else {
            load(serverUrl());
        }
    }

    // -- URL handling --------------------------------------------------------

    /** The configured base URL, falling back to the build-time default. */
    private String serverUrl() {
        return prefs.getString(KEY_URL, getString(R.string.default_server_url));
    }

    /** Normalise a base URL and open the control room. */
    private void load(String base) {
        web.loadUrl(dashboardUrl(base));
    }

    /** Turn whatever the user typed into a concrete {@code /dashboard} URL. */
    static String dashboardUrl(String base) {
        String url = base == null ? "" : base.trim();
        if (url.isEmpty()) {
            url = "http://127.0.0.1:8000";
        }
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "http://" + url;
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        if (!url.endsWith("/dashboard")) {
            url = url + "/dashboard";
        }
        return url;
    }

    private void showError(String detail) {
        String html = "<!doctype html><html><head>"
                + "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                + "<style>html,body{height:100%}body{margin:0;background:" + COLOR_DEEP
                + ";color:#e8f3ef;font-family:sans-serif;display:flex;align-items:center;"
                + "justify-content:center;text-align:center;padding:24px}"
                + "h2{margin:0 0 .4em}p{margin:.2em 0;opacity:.85}"
                + "small{opacity:.6}a{display:inline-block;margin-top:1.2em;padding:.7em 1.5em;"
                + "background:#a8e4d5;color:" + COLOR_DEEP
                + ";border-radius:999px;text-decoration:none;font-weight:700}</style></head><body>"
                + "<div><h2>Can't reach ActionMesh</h2><p>" + escape(detail) + "</p>"
                + "<p><small>" + escape(dashboardUrl(serverUrl())) + "</small></p>"
                + "<a href='actionmesh://retry'>Retry</a></div></body></html>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
    }

    private static String escape(String value) {
        if (value == null) {
            return "";
        }
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }

    private void showSettings() {
        final EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setText(serverUrl());
        new AlertDialog.Builder(this)
                .setTitle("ActionMesh server")
                .setMessage("Base URL of your ActionMesh server, e.g. http://192.168.1.10:8080")
                .setView(input)
                .setPositiveButton("Connect", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface dialog, int which) {
                        prefs.edit().putString(KEY_URL, input.getText().toString().trim()).apply();
                        load(serverUrl());
                    }
                })
                .setNegativeButton("Cancel", null)
                .show();
    }

    private void openInBrowser() {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(dashboardUrl(serverUrl()))));
        } catch (Exception exc) {
            Toast.makeText(this, "No browser available", Toast.LENGTH_SHORT).show();
        }
    }

    // -- Lifecycle -----------------------------------------------------------

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "Reload");
        menu.add(0, 2, 1, "Server\u2026");
        menu.add(0, 3, 2, "Open in browser");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        switch (item.getItemId()) {
            case 1:
                web.reload();
                return true;
            case 2:
                showSettings();
                return true;
            case 3:
                openInBrowser();
                return true;
            default:
                return super.onOptionsItemSelected(item);
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    // -- WebView glue --------------------------------------------------------

    private final class ShellClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri uri = request.getUrl();
            String scheme = uri.getScheme();
            if ("actionmesh".equals(scheme)) {
                if ("retry".equals(uri.getHost())) {
                    load(serverUrl());
                } else if ("settings".equals(uri.getHost())) {
                    showSettings();
                }
                return true;
            }
            if ("http".equals(scheme) || "https".equals(scheme)) {
                // Stay inside the app: the control room only ever talks to itself.
                return false;
            }
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, uri));
            } catch (Exception ignored) {
                // Nothing can handle it; keep the user in the app rather than crash.
            }
            return true;
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request,
                                    WebResourceError error) {
            if (request != null && request.isForMainFrame()) {
                String detail = error == null ? "" : String.valueOf(error.getDescription());
                showError(detail);
            }
        }
    }
}
