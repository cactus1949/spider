"""Render 1440x900 prototypes at device scale 2."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / "images"
OUT.mkdir(parents=True, exist_ok=True)
PAGES = {
    "personal.html": "personal.png",
    "personal-edit.html": "personal-edit.png",
    "system.html": "system.png",
    "file-detail.html": "file-detail.png",
    "alerts.html": "alerts.png",
    "state.html": "state-machine.png",
}

def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        for name, filename in PAGES.items():
            page.goto((ROOT / name).as_uri(), wait_until="load")
            page.evaluate("() => document.fonts.ready")
            page.wait_for_timeout(120)
            target = OUT / filename
            page.screenshot(path=str(target), type="png")
            print(filename, target.stat().st_size)
        browser.close()

if __name__ == "__main__":
    main()
