"""Render/CI build entry point. Run from the repository root."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from checker_backend.browser_runtime import BROWSERS_PATH, smoke_check


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-deps", action="store_true", help="Install OS packages (CI/root only)")
    args = parser.parse_args()
    requirements = Path(__file__).with_name("requirements.txt")
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(requirements)], check=True)
    command = [sys.executable, "-m", "playwright", "install", "--only-shell"]
    if args.with_deps:
        command.append("--with-deps")
    subprocess.run([*command, "chromium"], check=True, timeout=600)
    smoke_check()
    installed = sorted(path.name for path in BROWSERS_PATH.iterdir() if path.is_dir() and not path.name.startswith("."))
    size = sum(path.stat().st_size for path in BROWSERS_PATH.rglob("*") if path.is_file())
    print(f"Browser install: {', '.join(installed)}; {size / 1024**2:.1f} MiB", flush=True)
    print("Headless shell build check passed; no runtime browser download needed.", flush=True)


if __name__ == "__main__":
    main()
