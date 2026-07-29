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

BOT_DIR        = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT_FILE = os.path.join(BOT_DIR, "heartbeat.json")
EVENTS_FILE    = os.path.join(BOT_DIR, "events.json")

_hb_lock  = threading.Lock()
_evt_lock = threading.Lock()

MAX_EVENTS = 500

_CROSS_PROC_LOCK_TIMEOUT = 5.0   # seconds before a stale lock is broken
_CROSS_PROC_LOCK_POLL    = 0.02  # seconds between acquisition attempts


class _cross_process_lock:
    """Portable (stdlib-only) mutual-exclusion lock across the ~7 bot
    processes that share heartbeat.json/events.json. threading.Lock only
    protects against races between threads in the *same* process — two
    different bot processes can still both read the file, each apply their
    own change, and write it back, silently discarding one another's
    update (most damaging for log_event, where an entire logged event can
    be lost). This uses exclusive file creation as a spinlock, which works
    the same way on POSIX and Windows.
    """

    def __init__(self, path):
        self._lock_path = f"{path}.lock"

    def __enter__(self):
        deadline = time.time() + _CROSS_PROC_LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                if time.time() >= deadline:
                    # Break a stale lock left behind by a crashed process
                    # instead of deadlocking every bot forever.
                    try:
                        os.remove(self._lock_path)
                    except OSError:
                        pass
                    deadline = time.time() + _CROSS_PROC_LOCK_TIMEOUT
                    continue
                time.sleep(_CROSS_PROC_LOCK_POLL)

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            os.remove(self._lock_path)
        except OSError:
            pass


def _atomic_write(path, data):
    """Write JSON atomically using a PID-unique temp file + os.replace.
    PID-unique name prevents collisions when multiple bot processes write
    the same file concurrently (threading.Lock only protects within one process).
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


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
        "guild_id":  guild_id,
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
