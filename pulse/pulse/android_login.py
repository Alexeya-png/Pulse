"""Android WebView sign-in. Credentials and 2FA are entered on Instagram's page.

This is web-session authentication, not Instagram OAuth or an approved API.
No JavaScript bridge is injected; Python receives only the resulting session.
"""
from urllib.parse import urlparse

from .client import ORIGIN, parse_login_cookies


def open_instagram_login(on_session, on_cancel, on_error):
    from android.runnable import run_on_ui_thread
    from jnius import PythonJavaClass, java_method, autoclass
    from kivy.clock import Clock

    class Listener(PythonJavaClass):
        __javainterfaces__ = ["android/view/View$OnClickListener"]
        __javacontext__ = "app"

        @java_method("(Landroid/view/View;)V")
        def onClick(self, view):
            controller.finish()

    class DismissListener(PythonJavaClass):
        __javainterfaces__ = ["android/content/DialogInterface$OnDismissListener"]
        __javacontext__ = "app"

        @java_method("(Landroid/content/DialogInterface;)V")
        def onDismiss(self, dialog):
            controller.clean()
            if not controller.done:
                controller.done = True
                Clock.schedule_once(lambda _: on_cancel())

    class Controller:
        done = False
        dialog = None
        web = None

        @run_on_ui_thread
        def open(self):
            try:
                self.build()
            except Exception:
                self.done = True
                try:
                    if self.dialog:
                        self.dialog.dismiss()
                finally:
                    self.clean()
                    Clock.schedule_once(lambda _: on_error())

        def build(self):
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            self.dialog = autoclass("android.app.Dialog")(activity)
            self.dialog.setTitle("Подключение Instagram")
            layout = autoclass("android.widget.LinearLayout")(activity)
            layout.setOrientation(1)
            self.note = autoclass("android.widget.TextView")(activity)
            self.note.setText("Войдите на странице Instagram. После завершения входа нажмите «Готово».")
            self.note.setPadding(20, 16, 20, 16)
            layout.addView(self.note)
            self.web = autoclass("android.webkit.WebView")(activity)
            settings = self.web.getSettings()
            settings.setJavaScriptEnabled(True)
            settings.setDomStorageEnabled(True)
            settings.setAllowFileAccess(False)
            settings.setAllowContentAccess(False)
            settings.setMixedContentMode(1)  # MIXED_CONTENT_NEVER_ALLOW
            self.web.setWebViewClient(autoclass("android.webkit.WebViewClient")())
            self.cookies = autoclass("android.webkit.CookieManager").getInstance()
            self.cookies.setAcceptCookie(True)
            self.cookies.setAcceptThirdPartyCookies(self.web, False)
            params = autoclass("android.widget.LinearLayout$LayoutParams")(-1, 0, 1.0)
            layout.addView(self.web, params)
            button = autoclass("android.widget.Button")(activity)
            button.setText("Готово — проверить подключение")
            self.click_listener = Listener()
            button.setOnClickListener(self.click_listener)
            layout.addView(button)
            self.dismiss_listener = DismissListener()
            self.dialog.setOnDismissListener(self.dismiss_listener)
            self.dialog.setContentView(layout)
            self.dialog.show()
            self.dialog.getWindow().setLayout(-1, -1)
            self.web.loadUrl(ORIGIN + "/accounts/login/")

        def finish(self):
            if self.done:
                return
            try:
                current = urlparse(str(self.web.getUrl() or ""))
                if current.scheme != "https" or current.hostname not in {"instagram.com", "www.instagram.com"}:
                    self.note.setText("Вернитесь на страницу Instagram и завершите вход.")
                    return
                cookies = parse_login_cookies(str(self.cookies.getCookie(ORIGIN) or ""))
                settings = {"version": 2, "cookies": cookies, "user_agent": str(self.web.getSettings().getUserAgentString())}
            except Exception:
                self.note.setText("Вход ещё не завершён. Войдите в Instagram, подтвердите 2FA и нажмите «Готово» снова.")
                return
            self.done = True
            self.dialog.dismiss()
            Clock.schedule_once(lambda _: on_session(settings))

        def clean(self):
            # Keep the durable session only in our encrypted vault. WebView's
            # app-private cookie/storage databases are cleared on dismissal.
            if self.web:
                # A platform error in one cleanup step must not prevent the rest
                # or strand the Kivy sign-in controls in a disabled state.
                actions = [
                    lambda: self.web.stopLoading(),
                    lambda: self.web.clearCache(True),
                    lambda: self.web.clearHistory(),
                    lambda: self.cookies.removeAllCookies(None),
                    lambda: self.cookies.flush(),
                    lambda: autoclass("android.webkit.WebStorage").getInstance().deleteAllData(),
                    lambda: self.web.destroy(),
                ]
                for action in actions:
                    try:
                        action()
                    except Exception:
                        pass
                self.web = None

        @run_on_ui_thread
        def close(self):
            if self.dialog:
                self.dialog.dismiss()

    controller = Controller()
    controller.open()
    return controller
