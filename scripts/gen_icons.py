"""Render the PWA icons (clicker4ai/web/icon-{180,512}.png) from
assets/icons/clicker4ai.svg with the Playwright headless Chromium
already in ~/.cache/ms-playwright — no PIL, no npm package needed.

    .venv/bin/python scripts/gen_icons.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "assets" / "icons" / "clicker4ai.svg"
WEB = ROOT / "clicker4ai" / "web"
SIZES = (180, 512)


def chromium() -> Path:
    found = sorted(Path.home().glob(
        ".cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
    if not found:
        sys.exit("chrome-headless-shell not found under ~/.cache/ms-playwright")
    return found[-1]


def main() -> None:
    exe = chromium()
    with tempfile.TemporaryDirectory(dir=ROOT / ".scratch" if (ROOT / ".scratch").is_dir() else None) as tmp:
        for size in SIZES:
            page = Path(tmp) / f"icon-{size}.html"
            page.write_text(
                '<html><body style="margin:0;overflow:hidden">'
                f'<img src="{SVG.as_uri()}" width="{size}" height="{size}" style="display:block">'
                "</body></html>")
            out = WEB / f"icon-{size}.png"
            subprocess.run(
                [str(exe), "--no-sandbox", "--hide-scrollbars", "--force-device-scale-factor=1",
                 f"--window-size={size},{size}", f"--screenshot={out}", page.as_uri()],
                check=True, capture_output=True)
            print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
