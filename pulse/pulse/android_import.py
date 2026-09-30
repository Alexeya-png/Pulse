"""Android bridges for Instagram export and ZIP/JSON import."""
from kivy.clock import Clock

INSTAGRAM_PACKAGE = "com.instagram.android"
EXPORT_URL = "https://accountscenter.instagram.com/info_and_permissions/dyi/?source=external"


def configure_android_system_bars():
    if __import__("kivy.utils", fromlist=["platform"]).platform != "android":
        return

    from android.runnable import run_on_ui_thread
    from jnius import autoclass

    @run_on_ui_thread
    def _configure():
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        window = activity.getWindow()
        color = autoclass("android.graphics.Color")
        build = autoclass("android.os.Build$VERSION")
        view = autoclass("android.view.View")
        params = autoclass("android.view.WindowManager$LayoutParams")

        window.addFlags(params.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS)
        window.setNavigationBarColor(color.TRANSPARENT)

        decor = window.getDecorView()
        flags = decor.getSystemUiVisibility()
        flags |= view.SYSTEM_UI_FLAG_LAYOUT_STABLE
        flags |= view.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
        if build.SDK_INT >= 26:
            flags &= ~view.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
        decor.setSystemUiVisibility(flags)

        if build.SDK_INT >= 29:
            window.setNavigationBarContrastEnforced(False)

    _configure()


def open_instagram_export(on_error=None):
    """Open the installed Instagram app, reusing its current signed-in account.

    We first ask Instagram itself to handle the official Accounts Center export
    deep link. If that URL is not registered by the installed build, we still
    open Instagram rather than falling back to a browser/login page.
    """
    from android.runnable import run_on_ui_thread
    from jnius import autoclass

    @run_on_ui_thread
    def _open():
        try:
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            intent_cls = autoclass("android.content.Intent")
            uri_cls = autoclass("android.net.Uri")
            pm = activity.getPackageManager()

            intent = intent_cls(intent_cls.ACTION_VIEW, uri_cls.parse(EXPORT_URL))
            intent.setPackage(INSTAGRAM_PACKAGE)
            intent.addFlags(intent_cls.FLAG_ACTIVITY_NEW_TASK)

            if intent.resolveActivity(pm) is not None:
                activity.startActivity(intent)
                return

            launch = pm.getLaunchIntentForPackage(INSTAGRAM_PACKAGE)
            if launch is None:
                raise RuntimeError("Приложение Instagram не установлено.")
            launch.addFlags(intent_cls.FLAG_ACTIVITY_NEW_TASK)
            activity.startActivity(launch)
        except Exception as exc:
            if on_error is not None:
                Clock.schedule_once(lambda _, message=str(exc): on_error(message))

    _open()


def pick_instagram_export(on_file, on_cancel, on_error):
    from android.runnable import run_on_ui_thread
    from jnius import autoclass

    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    prefs = activity.getSharedPreferences("pulse_import", 0)

    class Controller:
        def __init__(self):
            self.event = None
            self.done = False

        @run_on_ui_thread
        def open(self):
            try:
                prefs.edit().clear().commit()
                intent = autoclass("android.content.Intent")(
                    activity,
                    autoclass("app.localtracker.pulse.PulseImportActivity"),
                )
                activity.startActivity(intent)
                self.event = Clock.schedule_interval(self.poll, 0.35)
            except Exception as exc:
                self.finish()
                Clock.schedule_once(lambda _: on_error(str(exc)))

        def finish(self):
            if self.event is not None:
                self.event.cancel()
                self.event = None

        def poll(self, _):
            if self.done:
                return False
            if prefs.getBoolean("cancelled", False):
                self.done = True
                prefs.edit().clear().apply()
                self.finish()
                on_cancel()
                return False

            error = str(prefs.getString("error", "") or "")
            if error:
                self.done = True
                prefs.edit().clear().apply()
                self.finish()
                on_error(error)
                return False

            if not prefs.getBoolean("done", False):
                return True

            path = str(prefs.getString("path", "") or "")
            self.done = True
            prefs.edit().clear().apply()
            self.finish()
            on_file(path)
            return False

    controller = Controller()
    controller.open()
    return controller
