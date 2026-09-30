"""Android bridge for the one-button Instagram WebView checker."""
from kivy.clock import Clock


def open_instagram_check(target, on_snapshot, on_cancel, on_error, on_progress):
    from android.runnable import run_on_ui_thread
    from jnius import autoclass

    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    prefs = activity.getSharedPreferences("pulse_instagram", 0)

    class Controller:
        def __init__(self):
            self.done = False
            self.event = None
            self.last_progress = ""

        @run_on_ui_thread
        def open(self):
            try:
                prefs.edit().clear().commit()
                intent = autoclass("android.content.Intent")(
                    activity,
                    autoclass("app.localtracker.pulse.PulseInstagramActivity"),
                )
                intent.putExtra("target", target)
                activity.startActivity(intent)
                self.event = Clock.schedule_interval(self.poll, 0.35)
            except Exception:
                self.finish()
                Clock.schedule_once(lambda _: on_error("Не удалось открыть Instagram."))

        def finish(self):
            if self.event is not None:
                self.event.cancel()
                self.event = None

        def poll(self, _):
            if self.done:
                return False

            progress = str(prefs.getString("progress", "") or "")
            if progress and progress != self.last_progress:
                self.last_progress = progress
                on_progress(progress)

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
            account = str(prefs.getString("account", "") or "")
            self.done = True
            prefs.edit().clear().apply()
            self.finish()
            on_snapshot(path, account)
            return False

        @run_on_ui_thread
        def close(self):
            self.done = True
            self.finish()
            try:
                autoclass("app.localtracker.pulse.PulseInstagramActivity").closeCurrent()
            except Exception:
                pass

    controller = Controller()
    controller.open()
    return controller
