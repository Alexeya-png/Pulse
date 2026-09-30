"""Android bridges for opening Meta export and importing Instagram ZIP/JSON."""
from kivy.clock import Clock

EXPORT_URL = "https://accountscenter.instagram.com/info_and_permissions/dyi/"


def open_instagram_export():
    from android.runnable import run_on_ui_thread
    from jnius import autoclass

    @run_on_ui_thread
    def _open():
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        intent_cls = autoclass("android.content.Intent")
        uri_cls = autoclass("android.net.Uri")
        intent = intent_cls(intent_cls.ACTION_VIEW, uri_cls.parse(EXPORT_URL))
        activity.startActivity(intent)

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
