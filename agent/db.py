"""Tiny Supabase (PostgREST) client + a background event sink."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Optional

import httpx

log = logging.getLogger("twingate.db")


class DB:
    def __init__(self, url: str, service_key: str) -> None:
        self.base = url.rstrip("/") + "/rest/v1"
        self.http = httpx.Client(
            timeout=10,
            headers={
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
        )

    def select(self, table: str, **params: str) -> list[dict[str, Any]]:
        r = self.http.get(f"{self.base}/{table}", params=params)
        r.raise_for_status()
        return r.json()

    def insert(self, table: str, row: dict[str, Any]) -> Optional[dict[str, Any]]:
        r = self.http.post(f"{self.base}/{table}", json=row)
        r.raise_for_status()
        data = r.json()
        return data[0] if data else None

    def update(self, table: str, match: dict[str, str], patch: dict[str, Any]) -> list[dict[str, Any]]:
        r = self.http.patch(f"{self.base}/{table}", params=match, json=patch)
        r.raise_for_status()
        return r.json()

    def upsert(self, table: str, row: dict[str, Any], on_conflict: str) -> None:
        r = self.http.post(
            f"{self.base}/{table}",
            params={"on_conflict": on_conflict},
            json=row,
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        r.raise_for_status()


class EventSink:
    """Writes events off the motion thread so robot timing never waits on Wi-Fi."""

    def __init__(self, db: DB) -> None:
        self.db = db
        self.q: "queue.Queue[dict[str, Any]]" = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def emit(self, mission_id: Optional[int], stage: str, message: str = "", data: Any = None) -> None:
        log.info("[%s] %s %s", stage, message, "" if data is None else str(data)[:160])
        self.q.put({"mission_id": mission_id, "stage": stage, "message": message[:2000], "data": data})

    def _run(self) -> None:
        while True:
            row = self.q.get()
            try:
                self.db.insert("events", row)
            except Exception as e:  # noqa: BLE001
                log.warning("event insert failed: %s", e)

    def flush(self, timeout: float = 5.0) -> None:
        import time

        end = time.time() + timeout
        while not self.q.empty() and time.time() < end:
            time.sleep(0.05)
