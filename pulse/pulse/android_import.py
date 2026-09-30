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


