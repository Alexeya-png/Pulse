package app.localtracker.pulse;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ActivityInfo;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.HashSet;
import java.util.Set;

public class PulseInstagramActivity extends Activity {
    private static PulseInstagramActivity current;
    private WebView webView;
    private TextView status;
    private SharedPreferences prefs;
    private boolean collecting = false;
    private String target;
    private String account;
    private int expectedFollowers = -1;
    private int expectedFollowing = -1;
    private final JSONArray followers = new JSONArray();
    private final JSONArray following = new JSONArray();

    public static void closeCurrent() {
        if (current != null) current.finish();
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        current = this;
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_PORTRAIT);
        prefs = getSharedPreferences("pulse_instagram", 0);
        prefs.edit().clear().commit();
        target = getIntent().getStringExtra("target");
        if (target == null) target = "";

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.BLACK);

        status = new TextView(this);
        status.setText("Войдите в Instagram");
        status.setTextColor(Color.WHITE);
        status.setTextSize(16);
        status.setPadding(24, 20, 24, 20);
        root.addView(status, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));

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

        android.webkit.CookieManager cm = android.webkit.CookieManager.getInstance();
        cm.setAcceptCookie(true);
        cm.setAcceptThirdPartyCookies(webView, true);

        webView.addJavascriptInterface(new Bridge(), "PulseBridge");
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                String host = uri.getHost();
                if (host == null) return true;
                host = host.toLowerCase();
                return !(host.equals("instagram.com") || host.endsWith(".instagram.com"));
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                if (!collecting) probeLogin();
            }
        });

        root.addView(webView, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(root);

        webView.loadUrl("https://www.instagram.com/accounts/login/");
    }

    private void probeLogin() {
        if (webView == null || collecting) return;
        String js =
                "(async()=>{try{" +
                "const r=await fetch('/api/v1/accounts/current_user/?edit=true',{" +
                "credentials:'include',headers:{'Accept':'application/json','X-IG-App-ID':'936619743392459','X-Requested-With':'XMLHttpRequest'}});" +
                "if(!r.ok)return;const d=await r.json();const u=d&&d.user;if(!u)return;" +
                "const id=String(u.pk||u.id||'');if(!id)return;" +
                "PulseBridge.authenticated(String(u.username||''),id);" +
                "}catch(e){}})();";
        webView.evaluateJavascript(js, null);
    }

    private String collectionScript(String username) {
        String safeTarget = JSONObject.quote(username);
        return "(async()=>{"
                + "const target=" + safeTarget + ";"
                + "const h={'Accept':'application/json','X-IG-App-ID':'936619743392459','X-Requested-With':'XMLHttpRequest'};"
                + "const csrf=((document.cookie.match(/(?:^|; )csrftoken=([^;]+)/)||[])[1]||'');if(csrf)h['X-CSRFToken']=decodeURIComponent(csrf);"
                + "async function api(path){const r=await fetch(path,{credentials:'include',headers:h});if(r.status===429)throw new Error('rate_limit');if(r.status===401||r.status===403)throw new Error('auth');if(!r.ok)throw new Error('http_'+r.status);return await r.json();}"
                + "try{"
                + "PulseBridge.progress('Проверяем профиль…');"
                + "const p=await api('/api/v1/users/web_profile_info/?username='+encodeURIComponent(target));"
                + "const u=p&&p.data&&p.data.user;if(!u||!u.id)throw new Error('profile');"
                + "const fid=String(u.id);"
                + "const fc=u.edge_followed_by&&u.edge_followed_by.count;const gc=u.edge_follow&&u.edge_follow.count;"
                + "if(!Number.isInteger(fc)||!Number.isInteger(gc))throw new Error('counts');"
                + "PulseBridge.beginSnapshot(target,fc,gc);"
                + "async function load(kind,expected,title){let cursor='';const seen=new Set();let total=0;"
                + "while(true){let path='/api/v1/friendships/'+fid+'/'+kind+'/?count=100';if(cursor)path+='&max_id='+encodeURIComponent(cursor);"
                + "const d=await api(path);if(!Array.isArray(d.users))throw new Error('list');"
                + "const page=[];for(const x of d.users){const id=String(x.pk||x.id||'');const un=String(x.username||'');if(id&&un)page.push({id:id,username:un});}"
                + "total+=page.length;PulseBridge.page(kind,JSON.stringify(page));PulseBridge.progress(title+': '+total+' / '+expected);"
                + "const next=String(d.next_max_id||'');if(!next)break;if(seen.has(next))throw new Error('cursor');seen.add(next);cursor=next;"
                + "}if(total!==expected)throw new Error('incomplete_'+kind);}"
                + "await load('followers',fc,'Подписчики');await load('following',gc,'Подписки');"
                + "const p2=await api('/api/v1/users/web_profile_info/?username='+encodeURIComponent(target));const u2=p2&&p2.data&&p2.data.user;"
                + "const fc2=u2&&u2.edge_followed_by&&u2.edge_followed_by.count;const gc2=u2&&u2.edge_follow&&u2.edge_follow.count;"
                + "if(fc2!==fc||gc2!==gc)throw new Error('changed');"
                + "PulseBridge.complete();"
                + "}catch(e){PulseBridge.failed(String(e&&e.message||e));}"
                + "})();";
    }

    private void updateStatus(final String text) {
        runOnUiThread(() -> {
            status.setText(text);
            prefs.edit().putString("progress", text).apply();
        });
    }

    private void fail(String message) {
        prefs.edit().putString("error", message).commit();
        runOnUiThread(this::finish);
    }

    private final class Bridge {
        @JavascriptInterface
        public void authenticated(String username, String userId) {
            if (collecting) return;
            collecting = true;
            account = username == null ? "" : username.trim().toLowerCase();
            String requested = target == null ? "" : target.trim().toLowerCase();
            String selected = requested.isEmpty() ? account : requested;
            updateStatus("Вход выполнен. Загружаем данные…");
            runOnUiThread(() -> {
                webView.setVisibility(View.INVISIBLE);
                webView.evaluateJavascript(collectionScript(selected), null);
            });
        }

        @JavascriptInterface
        public void progress(String message) {
            updateStatus(message);
        }

        @JavascriptInterface
        public void beginSnapshot(String username, int followersCount, int followingCount) {
            account = username == null ? "" : username.trim().toLowerCase();
            expectedFollowers = followersCount;
            expectedFollowing = followingCount;
        }

        @JavascriptInterface
        public void page(String kind, String payload) {
            try {
                JSONArray source = new JSONArray(payload);
                JSONArray dest = "followers".equals(kind) ? followers : following;
                Set<String> existing = new HashSet<>();
                for (int i = 0; i < dest.length(); i++) {
                    JSONObject item = dest.getJSONObject(i);
                    existing.add(item.optString("id"));
                }
                for (int i = 0; i < source.length(); i++) {
                    JSONObject item = source.getJSONObject(i);
                    String id = item.optString("id");
                    if (!id.isEmpty() && existing.add(id)) dest.put(item);
                }
            } catch (Exception exc) {
                fail("Не удалось обработать список Instagram.");
            }
        }

        @JavascriptInterface
        public void complete() {
            try {
                if (expectedFollowers < 0 || expectedFollowing < 0
                        || followers.length() != expectedFollowers
                        || following.length() != expectedFollowing) {
                    fail("Instagram вернул неполный список. Проверка отменена.");
                    return;
                }

                JSONObject root = new JSONObject();
                root.put("schema_version", 1);
                root.put("account", account);
                root.put("captured_at", Instant.now().toString());

                JSONObject f = new JSONObject();
                f.put("complete", true);
                f.put("identity", "id");
                f.put("expected_count", expectedFollowers);
                f.put("users", followers);
                root.put("followers", f);

                JSONObject g = new JSONObject();
                g.put("complete", true);
                g.put("identity", "id");
                g.put("expected_count", expectedFollowing);
                g.put("users", following);
                root.put("following", g);

                File dir = new File(getCacheDir(), "pulse");
                if (!dir.exists() && !dir.mkdirs()) throw new Exception("cache");
                File out = new File(dir, "instagram-snapshot.json");
                try (FileOutputStream stream = new FileOutputStream(out, false)) {
                    stream.write(root.toString().getBytes(StandardCharsets.UTF_8));
                    stream.flush();
                }

                prefs.edit()
                        .putBoolean("done", true)
                        .putString("path", out.getAbsolutePath())
                        .putString("account", account)
                        .commit();

                runOnUiThread(this::finishActivity);
            } catch (Exception exc) {
                fail("Не удалось сохранить результат проверки.");
            }
        }

        private void finishActivity() {
            finish();
        }

        @JavascriptInterface
        public void failed(String code) {
            String message;
            if ("rate_limit".equals(code)) {
                message = "Instagram временно ограничил запросы. Попробуйте позже.";
            } else if ("auth".equals(code)) {
                message = "Сессия Instagram не подтверждена. Войдите ещё раз.";
            } else if (code != null && code.startsWith("incomplete_")) {
                message = "Instagram вернул неполный список. Проверка отменена.";
            } else if ("changed".equals(code)) {
                message = "Список изменился во время проверки. Повторите позже.";
            } else {
                message = "Не удалось загрузить данные Instagram.";
            }
            fail(message);
        }
    }

    @Override
    public void onBackPressed() {
        if (!collecting) {
            prefs.edit().putBoolean("cancelled", true).commit();
        } else {
            prefs.edit().putString("error", "Проверка отменена.").commit();
        }
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.stopLoading();
            webView.removeJavascriptInterface("PulseBridge");
            webView.destroy();
            webView = null;
        }
        if (current == this) current = null;
        super.onDestroy();
    }
}
