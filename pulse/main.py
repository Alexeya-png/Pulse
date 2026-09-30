"""Run with python main.py. Android starts this module directly."""
import os

os.environ.setdefault("KIVY_NO_ARGS", "1")
os.environ.setdefault("KIVY_NO_CONSOLELOG", "1")

if __name__ == "__main__":
    import argparse
    from pulse.ui import PulseApp

    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--screenshot", help="Render a demo screen, save PNG, then exit")
    args = parser.parse_args()
    PulseApp(demo=args.demo or bool(args.screenshot), screenshot=args.screenshot).run()
