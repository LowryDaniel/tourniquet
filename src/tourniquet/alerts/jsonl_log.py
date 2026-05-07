"""Always-on JSONL audit log for every alert event."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
from datetime import datetime


async def write_jsonl(event: object, message: str) -> None:
    """Append one JSON line to ~/.tourniquet/alerts.jsonl.

    Always-on: no configuration required.
    Creates the directory if it does not exist.
    """
    await asyncio.to_thread(_write_jsonl_sync, event, message)


def _write_jsonl_sync(event: object, message: str) -> None:
    log_dir = pathlib.Path.home() / ".tourniquet"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "alerts.jsonl"

    raw = dataclasses.asdict(event)  # type: ignore[arg-type]
    if "today" in raw and hasattr(raw["today"], "isoformat"):
        raw["today"] = raw["today"].isoformat()

    record = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "message": message,
        "event": raw,
    }

    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
