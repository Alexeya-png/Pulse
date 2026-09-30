from __future__ import annotations

from typing import Callable

_ALLOWED_COOKIES = {"sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "rur"}
_active: dict[str, object] = {}


def _parse_cookie_header(raw: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in str(raw or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name in _ALLOWED_COOKIES and value.strip():
            result[name] = value.strip()
    return result


def open_instagram_login(
    on_success: Callable[[dict], None],
    on_cancel: Callable[[], None] | None = None,
) -> None:
    try:
        from kivy.clock import Clock
        from kivy.utils import platform
    except Exception as exc:
        raise RuntimeError("Instagram login UI is unavailable.") from exc

    if platform != "android":
        raise RuntimeError("Instagram login is available in the Android app.")

    from android.runnable import run_on_ui_thread
    from jnius import PythonJavaClass, autoclass, java_method

    class ClickListener(PythonJavaClass):
        __javainterfaces__ = ["android/view/View$OnClickListener"]
        __javacontext__ = "app"

        def __init__(self, callback):
            super().__init__()
            self.callback = callback

        @java_method("(Landroid/view/View;)V")
        def onClick(self, _view):
            self.callback()

    @run_on_ui_thread
    def show() -> None:
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        LinearLayout = autoclass("android.widget.LinearLayout")
        Button = autoclass("android.widget.Button")
        WebView = autoclass("android.webkit.WebView")
        CookieManager = autoclass("android.webkit.CookieManager")
        ViewGroupParams = autoclass("android.view.ViewGroup$LayoutParams")
        Toast = autoclass("android.widget.Toast")
        BuildVersion = autoclass("android.os.Build$VERSION")

        container = LinearLayout(activity)
        container.setOrientation(LinearLayout.VERTICAL)
        container.setBackgroundColor(autoclass("android.graphics.Color").rgb(12, 14, 20))

        toolbar = LinearLayout(activity)
        toolbar.setOrientation(LinearLayout.HORIZONTAL)
        cancel = Button(activity)
        cancel.setText("Отмена")
        done = Button(activity)
        done.setText("Готово")
        toolbar.addView(cancel, LinearLayout.LayoutParams(0, ViewGroupParams.WRAP_CONTENT, 1.0))
        toolbar.addView(done, LinearLayout.LayoutParams(0, ViewGroupParams.WRAP_CONTENT, 1.0))

        web = WebView(activity)
        settings = web.getSettings()
        settings.setJavaScriptEnabled(True)
        settings.setDomStorageEnabled(True)
        settings.setDatabaseEnabled(True)

        cookies = CookieManager.getInstance()
        cookies.setAcceptCookie(True)
        if BuildVersion.SDK_INT >= 21:
            cookies.setAcceptThirdPartyCookies(web, True)
        cookies.removeAllCookies(None)
        cookies.flush()

        container.addView(toolbar, LinearLayout.LayoutParams(ViewGroupParams.MATCH_PARENT, ViewGroupParams.WRAP_CONTENT))
        container.addView(web, LinearLayout.LayoutParams(ViewGroupParams.MATCH_PARENT, 0, 1.0))

        def close_overlay() -> None:
            parent = container.getParent()
            if parent is not None:
                parent.removeView(container)
            try:
                web.stopLoading()
                web.destroy()
            except Exception:
                pass
            _active.clear()

        def cancelled() -> None:
            close_overlay()
            if on_cancel:
                Clock.schedule_once(lambda _dt: on_cancel(), 0)

        def completed() -> None:
            raw = cookies.getCookie("https://www.instagram.com/")
            parsed = _parse_cookie_header(raw)
            if not parsed.get("sessionid"):
                Toast.makeText(
                    activity,
                    "Сначала войдите в Instagram, затем нажмите «Готово».",
                    Toast.LENGTH_LONG,
                ).show()
                return
            payload = {
                "cookies": parsed,
                "user_agent": str(settings.getUserAgentString() or ""),
            }
            cookies.flush()
            close_overlay()
            Clock.schedule_once(lambda _dt: on_success(payload), 0)

        cancel_listener = ClickListener(cancelled)
        done_listener = ClickListener(completed)
        cancel.setOnClickListener(cancel_listener)
        done.setOnClickListener(done_listener)
        _active.update({
            "container": container,
            "web": web,
            "cancel": cancel,
            "done": done,
            "cancel_listener": cancel_listener,
            "done_listener": done_listener,
        })

        activity.addContentView(
            container,
            ViewGroupParams(ViewGroupParams.MATCH_PARENT, ViewGroupParams.MATCH_PARENT),
        )
        web.loadUrl("https://www.instagram.com/accounts/login/")

    show()
