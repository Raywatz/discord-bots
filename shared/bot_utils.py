"""
bot_utils.py — Shared utilities for all Discord bots.
Stdlib only (no discord imports) so status_server.py can import this too.
Handles: heartbeats, event logging, event resolution.
Uses atomic writes (os.replace) to survive 7 concurrent processes.
"""

import json
import os
import time
import uuid
import threading
import contextlib

try:
    import fcntl
except ImportError:
    fcntl = None  # not available on Windows — falls back to in-process-only locking

BOT_DIR        = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT_FILE = os.path.join(BOT_DIR, "heartbeat.json")
EVENTS_FILE    = os.path.join(BOT_DIR, "events.json")

_hb_lock  = threading.Lock()
_evt_lock = threading.Lock()

MAX_EVENTS = 500


@contextlib.contextmanager
def _cross_process_lock(path):
    """Advisory file lock so the read-modify-write cycle in write_heartbeat/
    log_event/resolve_event is atomic across the several bot processes that
    share these files, not just within one process (threading.Lock alone
    only protects a single process from itself).
    """
    if fcntl is None:
        yield
        return
    lock_path = f"{path}.lock"
    with open(lock_path, "a") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _atomic_write(path, data):
    """Write JSON atomically using a PID-unique temp file + os.replace.
    PID-unique name prevents collisions when multiple bot processes write
    the same file concurrently (threading.Lock only protects within one process).
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(Exception):
            os.unlink(tmp)
        raise


def _safe_load(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def write_heartbeat(bot_name: str) -> None:
    """Update this bot's heartbeat timestamp. Safe to call from asyncio tasks."""
    with _hb_lock, _cross_process_lock(HEARTBEAT_FILE):
        data = _safe_load(HEARTBEAT_FILE) or {}
        if not isinstance(data, dict):
            data = {}
        data[bot_name] = time.time()
        _atomic_write(HEARTBEAT_FILE, data)


def log_event(
    bot: str,
    event_type: str,   # "error" | "rate_limit" | "user_report"
    message: str,
    guild_id: str = None,
) -> str:
    """Prepend an event to events.json. Returns the new event's UUID."""
    entry = {
        "id":        str(uuid.uuid4()),
        "bot":       bot,
        "type":      event_type,
        "message":   str(message)[:1000],
        "timestamp": time.time(),
        "resolved":  False,
        "guild_id":  str(guild_id) if guild_id is not None else None,
    }
    with _evt_lock, _cross_process_lock(EVENTS_FILE):
        events = _safe_load(EVENTS_FILE)
        if not isinstance(events, list):
            events = []
        events.insert(0, entry)
        events = events[:MAX_EVENTS]
        _atomic_write(EVENTS_FILE, events)
    return entry["id"]


def resolve_event(event_id: str) -> bool:
    """Mark an event as resolved. Returns True if found and updated."""
    with _evt_lock, _cross_process_lock(EVENTS_FILE):
        events = _safe_load(EVENTS_FILE)
        if not isinstance(events, list):
            return False
        found = False
        for e in events:
            if e.get("id") == event_id:
                e["resolved"] = True
                found = True
                break
        if found:
            _atomic_write(EVENTS_FILE, events)
        return found


def load_events(resolved=None) -> list:
    """
    Load events.json.
    resolved=None → all, resolved=False → unresolved only, resolved=True → resolved only.
    """
    with _evt_lock:
        events = _safe_load(EVENTS_FILE)
    if not isinstance(events, list):
        return []
    if resolved is None:
        return events
    return [e for e in events if e.get("resolved") == resolved]


def load_heartbeats() -> dict:
    """Return {bot_name: unix_timestamp} for all bots."""
    with _hb_lock:
        data = _safe_load(HEARTBEAT_FILE)
    return data if isinstance(data, dict) else {}
