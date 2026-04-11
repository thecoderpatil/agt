"""
agt_equities.screener.cache — File cache helpers + TTL logic for the screener.

Cache root: agt_desk_cache/screener/<category>/<key>.json

Each entry stores both `fetched_at` (ISO timestamp) and `data` (the payload).
Reads check the age against a per-call TTL and treat expired entries as
cache misses. Writes are atomic via temp-file + rename to avoid partial
state on crash.

Categories used by the screener:
  finnhub/profile2     — 24h TTL
  finnhub/metric       — 24h TTL
  finnhub/dividend2    — 24h TTL
  finnhub/candle       — 24h TTL  (when used)

This module is pure file I/O. It does not import httpx, asyncio, or any
agt_equities/telegram_bot module. Safe to import from anywhere.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Cache root — repo-relative, mirrors agt_desk_cache/corporate_intel layout
CACHE_ROOT = Path(__file__).resolve().parent.parent.parent / "agt_desk_cache" / "screener"


def _safe_key(key: str) -> str:
    """Sanitize a cache key for filesystem use.

    Tickers may contain '.' (e.g. BRK.B), but never path separators.
    Replace any character outside [A-Za-z0-9._-] with '_'.
    """
    out = []
    for ch in key:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def cache_path(category: str, key: str) -> Path:
    """Return the absolute path for a cache entry. Does NOT create parents."""
    safe_cat = category.replace("..", "_").strip("/\\")
    return CACHE_ROOT / safe_cat / f"{_safe_key(key)}.json"


def cache_get(category: str, key: str, ttl_seconds: int) -> dict | None:
    """Return cached data if present and not expired, else None.

    Args:
        category: subdirectory under CACHE_ROOT (e.g. 'finnhub/metric')
        key: filename stem (typically a ticker)
        ttl_seconds: max age in seconds; entries older than this return None

    Returns:
        The cached `data` payload, or None if missing/expired/corrupt.
        Corrupt entries log a warning and are treated as misses.
    """
    path = cache_path(category, key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("screener cache: corrupt entry %s: %s", path, exc)
        return None

    fetched_at_raw = entry.get("fetched_at")
    if not fetched_at_raw:
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched_at_raw)
    except (TypeError, ValueError):
        return None

    age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
    if age > ttl_seconds:
        return None

    return entry.get("data")


def cache_put(category: str, key: str, data: Any) -> None:
    """Atomically write a cache entry with a fresh `fetched_at` timestamp.

    Uses temp-file + rename to avoid partial writes if the process dies
    mid-write. Cache directory is created on demand.
    """
    path = cache_path(category, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("screener cache: mkdir failed for %s: %s", path.parent, exc)
        return

    entry = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    try:
        # Atomic write: temp file in same directory, then rename
        fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp_", suffix=".json", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning("screener cache: write failed for %s: %s", path, exc)


def cache_clear(category: str, key: str) -> bool:
    """Delete a cache entry. Returns True if deleted, False if absent.

    Used by tests. Production code should rely on TTL expiry.
    """
    path = cache_path(category, key)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("screener cache: unlink failed for %s: %s", path, exc)
        return False


def cache_age_seconds(category: str, key: str) -> float | None:
    """Return age of a cache entry in seconds, or None if absent/corrupt.

    Used by tests and diagnostics.
    """
    path = cache_path(category, key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            entry = json.load(f)
        fetched_at = datetime.fromisoformat(entry["fetched_at"])
        return (datetime.now(timezone.utc) - fetched_at).total_seconds()
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
