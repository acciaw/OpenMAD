#!/usr/bin/env python3
"""Serve the MAD68 HE live telemetry HUD on localhost.

    python tools/hud.py                  # serve and open a browser
    python tools/hud.py --port 9000
    python tools/hud.py --no-browser

The page shows per-key sensor travel as a heatmap, refreshed continuously. It
also offers profile switching, which uses volatile writes, instant, no flash
wear, reverted by unplugging the keyboard.

Binds to 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mad68.hud import serve  # noqa: E402

if getattr(sys, "_MEIPASS", None):
    REPO = Path(sys._MEIPASS)
else:
    REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--interval", type=float, default=0.03,
                    help="sampling period in seconds (default 0.03 = ~33 Hz)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    httpd, sampler = serve(REPO / "profiles", port=args.port, interval=args.interval)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"HUD serving at {url}")
    print("press keys on the keyboard to see them light up; Ctrl-C to stop")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        sampler.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
