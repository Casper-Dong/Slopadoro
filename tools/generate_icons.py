#!/usr/bin/env python3
"""Generate Chrome extension icons from catlogo.jpeg."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "catlogo.jpeg"
ICON_DIR = ROOT / "extension" / "icons"


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"missing source icon: {SOURCE}")

    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for size in (16, 48, 128):
        subprocess.run(
            [
                "sips",
                "-s",
                "format",
                "png",
                "--resampleHeightWidth",
                str(size),
                str(size),
                str(SOURCE),
                "--out",
                str(ICON_DIR / f"{size}.png"),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    main()
