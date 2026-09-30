"""A request-scoped headless shell; browser downloads belong to the build."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path

# Keep binaries in the deployed application, not the ephemeral /tmp directory.
BROWSERS_PATH = Path(__file__).resolve().parent / ".browsers"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_PATH)
logger = logging.getLogger("uvicorn.error")


@contextmanager
def browser_session():
    from playwright.sync_api import sync_playwright

    # Playwright's sync objects must be created and used on the same thread.
    # One browser per collection also releases its memory while the API is idle.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            # No channel or executable override: Playwright selects headless shell.
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            yield browser
        finally:
            browser.close()


def smoke_check() -> None:
    """Fail the build/startup if the shell cannot render a local page."""
    forbidden = [
        path.name for path in BROWSERS_PATH.iterdir()
        if path.name.startswith(("chromium-", "firefox-", "webkit-"))
    ] if BROWSERS_PATH.exists() else []
    if forbidden:
        raise RuntimeError("Non-minimal browser installation; clear the build cache.")
    with browser_session() as browser:
        page = browser.new_page()
        page.set_content("<title>Pulse browser check</title><p id='status'>ready</p>")
        if page.locator("#status").inner_text() != "ready":
            raise RuntimeError("Headless shell smoke check failed.")
        logger.info("Chromium headless shell smoke check passed (version %s)", browser.version)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    smoke_check()
