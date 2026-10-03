"""Render/CI build entry point. Run from the repository root."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-deps", action="store_true")
    parser.parse_args()

    requirements = Path(__file__).with_name("requirements.txt")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import fastapi, requests, uvicorn, curl_cffi; print('Online collector build check passed.')",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
