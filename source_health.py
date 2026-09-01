#!/usr/bin/env python3
"""Pipeline-global, persistent source circuit breaker."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SourceHealthRegistry:
    def __init__(self, path: str | Path, *, failure_threshold: int = 2, cooldown_seconds: int = 300) -> None:
        self.path = Path(path)
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, ValueError):
            self.data = {}

    def _row(self, source: str) -> dict[str, Any]:
        return self.data.setdefault(source, {"consecutive_failures": 0, "total_failures": 0, "total_successes": 0})

    def available(self, source: str, now: datetime | None = None) -> bool:
        until = self._row(source).get("disabled_until")
        if not until:
            return True
        try:
            return (now or _now()) >= datetime.fromisoformat(str(until))
        except ValueError:
            return True

    def success(self, source: str) -> None:
        row = self._row(source)
        row["consecutive_failures"] = 0
        row["total_successes"] = int(row.get("total_successes") or 0) + 1
        row["last_success"] = _now().isoformat()
        row.pop("disabled_until", None)
        self.save()

    def failure(self, source: str, reason: str, *, retry_after: float | None = None) -> None:
        row = self._row(source)
        row["consecutive_failures"] = int(row.get("consecutive_failures") or 0) + 1
        row["total_failures"] = int(row.get("total_failures") or 0) + 1
        row["last_failure"] = _now().isoformat()
        row["last_failure_reason"] = reason
        if row["consecutive_failures"] >= self.failure_threshold or retry_after is not None:
            cooldown = max(self.cooldown_seconds, int(retry_after or 0))
            row["disabled_until"] = (_now() + timedelta(seconds=cooldown)).isoformat()
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
