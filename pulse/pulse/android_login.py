"""Launch the dedicated Android Instagram sign-in activity and read its session."""
from .client import parse_login_cookies


def open_instagram_login(on_session, on_cancel, on_error):
    from android.runnable import run_on_ui_thread
    from jnius import autoclass
    from kivy.clock import Clock

    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    prefs = activity.getSharedPreferences("pulse_login", 0)

    class Controller:
        def __init__(self):
            self.done = False
            self.event = None

        @run_on_ui_thread
        def open(self):
            try:
                prefs.edit().clear().commit()
                intent = autoclass("android.content.Intent")(activity, autoclass("app.localtracker.pulse.PulseLoginActivity"))
                activity.startActivity(intent)
                self.event = Clock.schedule_interval(self.poll, 0.4)
            except Exception:
                self.finish_event()
                Clock.schedule_once(lambda _: on_error())

        def finish_event(self):
            if self.event is not None:
                self.event.cancel()
                self.event = None

        def poll(self, _):
            if self.done:
                return False
            try:
                if prefs.getBoolean("cancelled", False):
                    self.done = True
                    prefs.edit().clear().apply()
                    self.finish_event()
                    on_cancel()
                    return False
                if not prefs.getBoolean("done", False):
                    return True
                header = str(prefs.getString("cookies", "") or "")
                user_agent = str(prefs.getString("user_agent", "") or "")
                cookies = parse_login_cookies(header)
                settings = {"version": 2, "cookies": cookies, "user_agent": user_agent}
                self.done = True
                prefs.edit().clear().apply()
                self.finish_event()
                on_session(settings)
                return False
            except Exception:
                self.done = True
                prefs.edit().clear().apply()
                self.finish_event()
                on_error()
                return False

        @run_on_ui_thread
        def close(self):
            self.done = True
            self.finish_event()
            try:
                autoclass("app.localtracker.pulse.PulseLoginActivity").closeCurrent()
            except Exception:
                pass

    controller = Controller()
    controller.open()
    return controller
