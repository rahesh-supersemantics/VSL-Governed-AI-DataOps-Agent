"""Atomic, history-preserving JSON report writing for the data-quality tooling.

A report is serialised first (so a non-serialisable value cannot leave a half-written file), written to a temporary
file in the same directory, flushed to disk and moved into place with ``os.replace``. A reader therefore sees either
the complete previous report or the complete new one. If a report already exists, a timestamped copy is kept first.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _archive_name(path: Path) -> Path:
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}.{stamp}-{counter}{path.suffix}")
        counter += 1
    return candidate


def write_json_atomic(path: str | Path, payload: Any, *, keep_previous: bool = True) -> Path | None:
    """Write ``payload`` to ``path`` atomically. Returns the path of the preserved previous report, if any."""
    target = Path(path)
    body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"  # serialise before touching the filesystem
    target.parent.mkdir(parents=True, exist_ok=True)

    archived: Path | None = None
    if keep_previous and target.is_file():
        archived = _archive_name(target)
        shutil.copy2(target, archived)

    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return archived
