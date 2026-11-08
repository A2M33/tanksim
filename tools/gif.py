"""Record docs/demo.gif from a freshly started tanksim.

    tanksim --no-browser --speed 60 &
    python tools/gif.py docs/demo.gif

Needs Playwright and Pillow.
"""

import io
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

URL = "http://localhost:8080/"
GREY_TEXT = ["--disable-lcd-text"]  # no coloured subpixel fringes on small grey labels
FRAME_MS = 200
WIDTH = 960
SIGNAL = ["#d99211", "#eddcba", "#b8322a"]  # amber, amber halo on panel, fault
CLIP = {"x": 0, "y": 0, "width": 1440, "height": 760}  # header, process and faceplate


def main(out: Path) -> None:
    frames: list[Image.Image] = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, args=GREY_TEXT)
        except Exception:
            browser = p.chromium.launch(headless=True, channel="msedge", args=GREY_TEXT)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(URL)
        page.wait_for_timeout(1500)

        def record(seconds: float) -> None:
            for _ in range(int(seconds * 1000 / FRAME_MS)):
                shot = page.screenshot(clip=CLIP)
                img = Image.open(io.BytesIO(shot)).convert("RGB")
                frames.append(img.resize((WIDTH, round(WIDTH * CLIP["height"] / CLIP["width"])), Image.LANCZOS))
                page.wait_for_timeout(FRAME_MS)

        # Raise the start level just under the current level so the pump starts soon.
        level = float(page.text_content("#v-level").split()[0])
        page.fill("#sp-start", str(int(level) - 3))
        page.click("text=Apply setpoints")
        record(9)  # tank drains, pump starts, tank fills
        # dispatch_event clicks without scrolling the page out of the clip
        page.locator("text=Break the pump").dispatch_event("click")
        record(5)  # fault latches, alarm flashes
        page.locator("text=Break the pump").dispatch_event("click")
        page.locator("text=Reset faults").dispatch_event("click")
        page.locator("text=Acknowledge alarms").dispatch_event("click")
        record(4)  # pump restarts after the restart delay
        browser.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    # Build one palette from frames across the run so the alarm red is in it.
    sample = frames[::10]
    sheet = Image.new("RGB", (WIDTH, sum(f.height for f in sample)))
    for i, f in enumerate(sample):
        sheet.paste(f, (0, i * f.height))
    palette = sheet.quantize(colors=64 - len(SIGNAL), method=Image.Quantize.MEDIANCUT)
    # The lamp is too small to win a palette slot on its own, so the signal
    # colours from tokens.css (light theme) are added by hand.
    colours = palette.getpalette()[: (64 - len(SIGNAL)) * 3]
    for hex_colour in SIGNAL:
        colours += [int(hex_colour[i:i + 2], 16) for i in (1, 3, 5)]
    palette = Image.new("P", (1, 1))
    palette.putpalette(colours)
    gif_frames = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    gif_frames[0].save(out, save_all=True, append_images=gif_frames[1:], duration=FRAME_MS, loop=0, optimize=True)
    print(f"{out}: {len(frames)} frames, {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "docs/demo.gif"))
