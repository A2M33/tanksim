"""Render tools/banner/banner.html to a 1280x640 PNG with Playwright and Microsoft Edge.
The same PNG serves as the README banner and the GitHub social preview.

    pip install playwright
    python tools/banner/render.py [out.png]
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent


def main(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", args=["--disable-lcd-text"])
        page = browser.new_page(viewport={"width": 1280, "height": 640}, color_scheme="dark")
        page.goto((HERE / "banner.html").as_uri())
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(200)
        page.screenshot(path=str(out))
        browser.close()
    print(out)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent.parent / "docs" / "banner.png")
