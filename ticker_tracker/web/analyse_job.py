"""Background portfolio analyse jobs for the dashboard (progress polling)."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from time import time
from typing import Any

_MAX_LOG_LINES = 200


class AnalyseJobStore:
    """Thread-safe in-memory job state for localhost dashboard runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def create(self) -> str:
        job_id = str(uuid.uuid4())
        with self._lock:
            self._jobs[job_id] = {
                "status": "queued",
                "pct": 0,
                "message": "Queued…",
                "log": [],
                "live_signals": [],
                "preview_html": None,
                "started_at": time(),
                "finished_at": None,
                "result": None,
                "error": None,
            }
        return job_id

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {
                "status": job["status"],
                "pct": job["pct"],
                "message": job["message"],
                "log": list(job["log"]),
                "live_signals": list(job["live_signals"]),
                "preview_html": job.get("preview_html"),
                "html": (job.get("result") or {}).get("html") if job.get("result") else None,
                "summary": (job.get("result") or {}).get("summary") if job.get("result") else None,
                "metadata": (
                    (job.get("result") or {}).get("metadata") if job.get("result") else None
                ),
                "error": job.get("error"),
            }

    def append_log(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["log"].append(line)
            if len(job["log"]) > _MAX_LOG_LINES:
                job["log"] = job["log"][-_MAX_LOG_LINES:]

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(fields)

    def progress_callback(self, job_id: str) -> Callable[[int, str], None]:
        def _cb(pct: int, msg: str) -> None:
            line = f"[{pct:3d}%] {msg}"
            self.update(job_id, status="running", pct=pct, message=msg)
            self.append_log(job_id, line)

        return _cb

    def set_preview_html(self, job_id: str, html: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.get("preview_html") == html:
                return
            job["preview_html"] = html

    def add_live_signal(
        self,
        job_id: str,
        *,
        ticker: str,
        signal: str,
        confidence: str,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            rows = job["live_signals"]
            for row in rows:
                if row.get("ticker") == ticker:
                    row["signal"] = signal
                    row["confidence"] = confidence
                    return
            rows.append(
                {
                    "ticker": ticker,
                    "signal": signal,
                    "confidence": confidence,
                }
            )

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        self.update(
            job_id,
            status="done",
            pct=100,
            message="Done.",
            result=result,
            finished_at=time(),
        )

    def fail(self, job_id: str, error: str) -> None:
        self.update(
            job_id,
            status="error",
            message=error,
            error=error,
            finished_at=time(),
        )


JOB_STORE = AnalyseJobStore()
