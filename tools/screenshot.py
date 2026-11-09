"""Take the README screenshots from a running tanksim.

    tanksim --no-browser --speed 60 &
    python tools/screenshot.py docs 120   # let two simulated hours pass first

Needs Playwright (pip install playwright && playwright install chromium).
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "http://localhost:8080/"
GREY_TEXT = ["--disable-lcd-text"]  # no coloured subpixel fringes on small grey labels


def main(out: Path, warmup_s: float = 0) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, args=GREY_TEXT)
        except Exception:  # no bundled Chromium; Windows always has Edge
            browser = p.chromium.launch(headless=True, channel="msedge", args=GREY_TEXT)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1, color_scheme="dark")
        errors = []
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(URL)
        page.wait_for_timeout(1500 + warmup_s * 1000)
        page.screenshot(path=out / "hmi.png", full_page=True)

        # Break the pump while it runs, to show an alarm.
        page.click("text=Manual")
        page.click("#btn-start")
        page.wait_for_timeout(1000)
        page.click("text=Break the pump")
        page.wait_for_timeout(2500)
        page.screenshot(path=out / "hmi-fault.png", full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(500)
        page.screenshot(path=out / "hmi-mobile.png", full_page=True)

        # Leave the plant as we found it.
        page.click("text=Break the pump")
        page.click("text=Reset faults")
        page.click("text=Acknowledge alarms")
        page.click("text=Auto")
        browser.close()
    if errors:
        print("Browser errors:", *errors, sep="\n  ")
        sys.exit(1)


if __name__ == "__main__":
    main(
        Path(sys.argv[1] if len(sys.argv) > 1 else "docs"),
        float(sys.argv[2]) if len(sys.argv) > 2 else 0,
    )
