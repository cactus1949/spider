"""Render design prototypes at 1440x900, device scale 2."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / "images"
OUT.mkdir(parents=True, exist_ok=True)
PAGES = {
    "home.html": "home-readiness.png",
    "catchup.html": "catchup.png",
    "attention.html": "attention.png",
    "plan.html": "plan.png",
    "onboarding.html": "onboarding.png",
    "flow.html": "flow.png",
    "visual.html": "visual-spec.png",
}

def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=2,
        )
        for name, filename in PAGES.items():
            page.goto((ROOT / name).as_uri(), wait_until="load")
            page.evaluate("() => document.fonts.ready")
            page.wait_for_timeout(150)
            target = OUT / filename
            page.screenshot(path=str(target), type="png")
            print(target.name, target.stat().st_size)
        browser.close()

if __name__ == "__main__":
    main()
