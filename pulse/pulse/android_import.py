"""Android UI helpers."""
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
