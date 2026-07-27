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


def watch_bot(bot_file: str) -> None:
    delay   = RESTART_DELAY
    bot_log = os.path.join(LOG_DIR, bot_file.replace(".py", ".log"))

    while not _stop.is_set():
        wdlog.info(f"Starting {bot_file} → logs/{os.path.basename(bot_log)}")
        start = time.time()

        with open(bot_log, "a", encoding="utf-8") as lf:
            lf.write(f"\n--- Started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            lf.flush()
            proc = subprocess.Popen(
                [PYTHON, bot_file],
                cwd=BOT_DIR,
                stdout=lf,
                stderr=lf,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            with _lock:
                _processes[bot_file] = proc
                if _stop.is_set():
                    # Shutdown was triggered while this process was starting, so it
                    # was missed by _shutdown()'s iteration over _processes. Kill it
                    # now instead of leaving it running as an orphan.
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            proc.wait()

        if _stop.is_set():
            break

        uptime = time.time() - start
        delay  = RESTART_DELAY if uptime >= STABLE_UPTIME else min(delay * 2, MAX_RESTART_DELAY)
        wdlog.warning(f"{bot_file} exited after {uptime:.0f}s (code {proc.returncode}). Restarting in {delay}s…")
        _stop.wait(timeout=delay)

# ── Shutdown ──────────────────────────────────────────────────────────────────


def _shutdown(signum, frame) -> None:
    wdlog.info("Shutting down…")
    _stop.set()
    with _lock:
        procs = list(_processes.items())

    # Signal every child first, then wait for exits. Don't hold _lock while
    # waiting (that can take seconds) — other threads only need it briefly.
    for name, proc in procs:
        try:
            proc.terminate()
        except Exception as e:
            wdlog.warning(f"Could not terminate {name}: {e}")

    for name, proc in procs:
        try:
            proc.wait(timeout=10)
            wdlog.info(f"Terminated {name}")
        except subprocess.TimeoutExpired:
            wdlog.warning(f"{name} did not exit in time, killing...")
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception as e:
                wdlog.warning(f"Could not kill {name}: {e}")
        except Exception:
            pass
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
