"""Dashboard analyse job store tests."""

from __future__ import annotations

import pytest

pytest.importorskip("flask")

from ticker_tracker.config import EncryptedConfig
from ticker_tracker.web.analyse_job import AnalyseJobStore
from ticker_tracker.web.dashboard_server import create_dashboard_app


def test_analyse_job_store_progress_and_complete() -> None:
    store = AnalyseJobStore()
    job_id = store.create()
    cb = store.progress_callback(job_id)
    cb(10, "Reading holdings…")
    cb(62, "Summary ready")
    store.set_preview_html(job_id, "<html>interim</html>")
    store.add_live_signal(job_id, ticker="AAPL", signal="BUY", confidence="High")
    snap = store.snapshot(job_id)
    assert snap is not None
    assert snap["pct"] == 62
    assert snap["preview_html"] == "<html>interim</html>"
    assert len(snap["live_signals"]) == 1
    store.complete(job_id, {"html": "<html>final</html>", "summary": {"emails_sent": 0}})
    done = store.snapshot(job_id)
    assert done is not None
    assert done["status"] == "done"
    assert done["html"] == "<html>final</html>"


def test_analyse_status_unknown_job(tmp_path) -> None:
    enc = EncryptedConfig(tmp_path / "config.enc")
    app = create_dashboard_app(enc)
    client = app.test_client()
    resp = client.get("/api/analyse/status/not-a-real-id")
    assert resp.status_code == 404
