package app.localtracker.pulse;

import android.app.Activity;
import android.content.pm.ActivityInfo;
import android.graphics.Color;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

public class PulseLoginActivity extends Activity {
    private static PulseLoginActivity current;
    private WebView webView;
    private TextView status;

    public static void closeCurrent() {
        if (current != null) current.finish();
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        current = this;
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_PORTRAIT);
        getSharedPreferences("pulse_login", 0).edit().clear().commit();

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.BLACK);

        webView = new WebView(this);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setSupportMultipleWindows(false);

        String ua = settings.getUserAgentString();
        if (ua != null) {
            ua = ua.replace("; wv", "").replace("Version/4.0 ", "");
            settings.setUserAgentString(ua);
        }

        webView.setWebViewClient(new WebViewClient());

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, true);

        root.addView(webView, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1.0f));

        status = new TextView(this);
        status.setText("");
        status.setTextColor(Color.rgb(255, 120, 150));
        status.setGravity(Gravity.CENTER);
        status.setPadding(24, 8, 24, 8);
        root.addView(status, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        Button done = new Button(this);
        done.setText("Готово");
        done.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                finishLogin();
            }
        });
        root.addView(done, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        setContentView(root);
        webView.loadUrl("https://www.instagram.com/accounts/login/");
    }

    private void finishLogin() {
        CookieManager manager = CookieManager.getInstance();
        String raw = manager.getCookie("https://www.instagram.com");

        if (raw == null
                || !raw.contains("sessionid=")
                || !raw.contains("csrftoken=")
                || !raw.contains("ds_user_id=")) {
            status.setText("Сначала завершите вход в Instagram");
            return;
        }

        String ua = webView.getSettings().getUserAgentString();
        getSharedPreferences("pulse_login", 0).edit()
                .putBoolean("done", true)
                .putString("cookies", raw)
                .putString("user_agent", ua == null ? "" : ua)
                .commit();

        manager.removeAllCookies(null);
        manager.flush();
        finish();
    }

    @Override
    public void onBackPressed() {
        getSharedPreferences("pulse_login", 0).edit()
                .putBoolean("cancelled", true)
                .commit();
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.stopLoading();
            webView.clearHistory();
            webView.destroy();
            webView = null;
        }
        if (current == this) current = null;
        super.onDestroy();
    }
}
