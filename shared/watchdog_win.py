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

BOTS: list[tuple[str, str]] = [
    ("yt-music-bot", "yt_music_bot.py"),
]

# ── Restart policy ────────────────────────────────────────────────────────────

RESTART_DELAY     = 3
MAX_RESTART_DELAY = 120
STABLE_UPTIME     = 60

# ── Paths ─────────────────────────────────────────────────────────────────────

SHARED_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(SHARED_DIR)
LOG_DIR    = os.path.join(SHARED_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Windows venv uses Scripts\python.exe instead of bin/python3
_venv_py = os.path.join(SHARED_DIR, ".venv", "Scripts", "python.exe")
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


def watch_bot(bot_subdir: str, bot_file: str) -> None:
    delay    = RESTART_DELAY
    bot_log  = os.path.join(LOG_DIR, bot_file.replace(".py", ".log"))
    bot_dir  = os.path.join(REPO_ROOT, bot_subdir)  # each bot lives in its own subdirectory, not shared/
    bot_path = os.path.join(bot_dir, bot_file)
    if not os.path.exists(bot_path):
        wdlog.error(f"{bot_path} does not exist — skipping {bot_file}.")
        return

    while not _stop.is_set():
        start = time.time()
        try:
            wdlog.info(f"Starting {bot_file} → logs/{os.path.basename(bot_log)}")

            with open(bot_log, "a", encoding="utf-8") as lf:
                lf.write(f"\n--- Started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                lf.flush()
                proc = subprocess.Popen(
                    [PYTHON, bot_path],
                    cwd=bot_dir,
                    stdout=lf,
                    stderr=lf,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                with _lock:
                    _processes[bot_file] = proc
                proc.wait()
            returncode = proc.returncode
        except Exception as e:
            # Spawn-time errors must not kill this thread — that would leave
            # the bot unsupervised forever with no further warning.
            wdlog.error(f"Error supervising {bot_file}: {e}")
            returncode = None

        if _stop.is_set():
            break

        uptime = time.time() - start
        delay  = RESTART_DELAY if uptime >= STABLE_UPTIME else min(delay * 2, MAX_RESTART_DELAY)
        wdlog.warning(f"{bot_file} exited after {uptime:.0f}s (code {returncode}). Restarting in {delay}s…")
        _stop.wait(timeout=delay)

# ── Shutdown ──────────────────────────────────────────────────────────────────

_shutting_down = False


def _shutdown(signum, frame) -> None:
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
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
wdlog.info(f"  Repo root : {REPO_ROOT}")
wdlog.info(f"  Log dir : {LOG_DIR}")
wdlog.info(f"  Python  : {PYTHON}")
wdlog.info(f"  Bots    : {', '.join(bot for _, bot in BOTS)}")
wdlog.info("=" * 55)

_check = subprocess.run([PYTHON, "-c", "import discord"], capture_output=True, cwd=SHARED_DIR)
if _check.returncode != 0:
    wdlog.error("discord.py not found. Run:")
    wdlog.error(f'  {PYTHON} -m pip install "discord.py[voice]" yt-dlp PyNaCl')
    sys.exit(1)

for bot_subdir, bot_file in BOTS:
    t = threading.Thread(target=watch_bot, args=(bot_subdir, bot_file), daemon=True)
    t.start()

_stop.wait()
