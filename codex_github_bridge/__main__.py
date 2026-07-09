from __future__ import annotations

import argparse
import signal

from .app import BridgeApp
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll GitHub issue comments and control Codex CLI.")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    args = parser.parse_args()

    cfg = load_config()
    app = BridgeApp(cfg)
    signal.signal(signal.SIGINT, app.stop)
    signal.signal(signal.SIGTERM, app.stop)
    if args.once:
        app.run_once()
    else:
        app.run_forever()


if __name__ == "__main__":
    main()
