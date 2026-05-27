"""Launch the local web dashboard (Flask on port 5225 by default)."""

from __future__ import annotations

import os

from ticker_tracker.config import load_env_files
from ticker_tracker.web.dashboard_server import run_dashboard_server


def main() -> None:
    load_env_files()
    host = os.environ.get("TICKER_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("TICKER_DASHBOARD_PORT", "5225"))
    run_dashboard_server(host=host, port=port)


if __name__ == "__main__":
    main()
