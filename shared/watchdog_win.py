# watchdog_win.py — Windows watchdog for yt_music_bot.py
#
# Run this on a Windows machine instead of the Mac watchdog.
# Supervises yt_music_bot.py, restarts it on crash, writes logs.
#
# Usage:
#   python watchdog_win.py
#
# Stop: Ctrl+C in the terminal.

import subprocess
import time
import threading
import os
import sys
import signal
import logging
from logging.handlers import RotatingFileHandler

# ── Bots to manage ────────────────────────────────────────────────────────────

BOTS: list[str] = [
    "yt_music_bot.py",
]

# ── Restart policy ────────────────────────────────────────────────────────────

RESTART_DELAY     = 3
MAX_RESTART_DELAY = 120
STABLE_UPTIME     = 60

# ── Paths ─────────────────────────────────────────────────────────────────────

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BOT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Each bot actually lives in its own sibling directory (e.g. yt-music-bot\yt_music_bot.py),
# not inside shared\ itself — resolve the real directory per bot before spawning.
REPO_ROOT = os.path.dirname(BOT_DIR)

# Windows venv uses Scripts\python.exe instead of bin/python3
_venv_py = os.path.join(BOT_DIR, ".venv", "Scripts", "python.exe")
PYTHON   = _venv_py if os.path.exists(_venv_py) else sys.executable

# ── Watchdog logger ───────────────────────────────────────────────────────────

_fmt = logging.Formatter("[%(asctime)s] [%(levelname)-8s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_fh  = RotatingFileHandler(
    os.path.join(LOG_DIR, "watchdog_win.log"),
    maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
wdlog = logging.getLogger("watchdog_win")
wdlog.setLevel(logging.INFO)
wdlog.addHandler(_fh)
wdlog.addHandler(_sh)

# ── Shared state ──────────────────────────────────────────────────────────────

_processes: dict[str, subprocess.Popen] = {}
_lock  = threading.Lock()
_stop  = threading.Event()

# ── Supervisor ────────────────────────────────────────────────────────────────


def _locate_bot(bot_file: str) -> str:
    """Return the directory that actually contains bot_file.
    Checks shared\\ first (legacy layout), then every sibling directory
    under the repo root. Falls back to BOT_DIR if not found anywhere so
    the failure surfaces clearly instead of silently crash-looping."""
    if os.path.isfile(os.path.join(BOT_DIR, bot_file)):
        return BOT_DIR
    try:
        entries = os.listdir(REPO_ROOT)
    except OSError:
        entries = []
    for entry in entries:
        candidate_dir = os.path.join(REPO_ROOT, entry)
        if os.path.isfile(os.path.join(candidate_dir, bot_file)):
            return candidate_dir
    return BOT_DIR


def watch_bot(bot_file: str) -> None:
    delay   = RESTART_DELAY
    bot_log = os.path.join(LOG_DIR, bot_file.replace(".py", ".log"))
    run_dir = _locate_bot(bot_file)

    while not _stop.is_set():
        wdlog.info(f"Starting {bot_file} → logs/{os.path.basename(bot_log)}")
        start = time.time()
        proc = None

        with open(bot_log, "a", encoding="utf-8") as lf:
            lf.write(f"\n--- Started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            lf.flush()
            try:
                proc = subprocess.Popen(
                    [PYTHON, bot_file],
                    cwd=run_dir,
                    stdout=lf,
                    stderr=lf,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                with _lock:
                    _processes[bot_file] = proc
                proc.wait()
            except Exception as e:
                # Without this, a failure to even launch the process (bad
                # path, permission error, etc.) would raise out of this
                # thread and permanently stop supervising this bot.
                lf.write(f"Failed to start/run {bot_file}: {e}\n")
                wdlog.error(f"Failed to start/run {bot_file}: {e}")

        if _stop.is_set():
            break

        uptime = time.time() - start
        delay  = RESTART_DELAY if uptime >= STABLE_UPTIME else min(delay * 2, MAX_RESTART_DELAY)
        returncode = proc.returncode if proc is not None else "n/a"
        wdlog.warning(f"{bot_file} exited after {uptime:.0f}s (code {returncode}). Restarting in {delay}s…")
        _stop.wait(timeout=delay)

# ── Shutdown ──────────────────────────────────────────────────────────────────


def _shutdown(signum, frame) -> None:
    wdlog.info("Shutting down…")
    _stop.set()
    with _lock:
        for name, proc in _processes.items():
            try:
                proc.terminate()
                wdlog.info(f"Terminated {name}")
            except Exception as e:
                wdlog.warning(f"Could not terminate {name}: {e}")
    sys.exit(0)


signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _shutdown)

# ── Startup ───────────────────────────────────────────────────────────────────

wdlog.info("=" * 55)
wdlog.info("Windows Watchdog started")
wdlog.info(f"  Bot dir : {BOT_DIR}")
wdlog.info(f"  Log dir : {LOG_DIR}")
wdlog.info(f"  Python  : {PYTHON}")
wdlog.info(f"  Bots    : {', '.join(BOTS)}")
wdlog.info("=" * 55)

_check = subprocess.run([PYTHON, "-c", "import discord"], capture_output=True, cwd=BOT_DIR)
if _check.returncode != 0:
    wdlog.error("discord.py not found. Run:")
    wdlog.error(f'  {PYTHON} -m pip install "discord.py[voice]" yt-dlp PyNaCl')
    sys.exit(1)

for bot_file in BOTS:
    t = threading.Thread(target=watch_bot, args=(bot_file,), daemon=True)
    t.start()

_stop.wait()
