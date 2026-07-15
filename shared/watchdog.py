import subprocess
import time
import threading
import os
import sys
import signal

BOTS = [
    "counting_bot.py",
    "file_uploader_bot.py",
    "moderation_bot.py",
    "inbox_bot.py",
    "hub_bot.py",
    "vibe_bot.py",
    "games_bot.py",
    "python_bot.py",
    "yt_music_bot.py",
]

RESTART_DELAY     = 3    # initial delay (seconds)
MAX_RESTART_DELAY = 120  # cap backoff at 2 minutes
STABLE_UPTIME     = 60   # if bot runs > this many seconds, reset backoff

# Always run bots from the directory containing this script
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BOT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Prefer the local venv Python (has discord.py installed) over the system Python
_venv_python = os.path.join(BOT_DIR, ".venv", "bin", "python3")
PYTHON = _venv_python if os.path.exists(_venv_python) else sys.executable

_processes = {}  # bot_file -> subprocess.Popen
_lock = threading.Lock()
_stop = threading.Event()


def watch_bot(bot_file):
    delay = RESTART_DELAY
    while not _stop.is_set():
        start_time = time.time()
        try:
            log_path = os.path.join(LOG_DIR, bot_file.replace(".py", ".log"))
            print(f"[watchdog] Starting {bot_file} (log: logs/{bot_file.replace('.py', '.log')})...")

            with open(log_path, "a") as log:
                log.write(f"\n--- Started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                log.flush()
                process = subprocess.Popen(
                    [PYTHON, bot_file],
                    cwd=BOT_DIR,
                    stdout=log,
                    stderr=log
                )
                with _lock:
                    _processes[bot_file] = process
                process.wait()
            returncode = process.returncode
        except Exception as e:
            # Spawn-time errors (e.g. transient FileNotFoundError/OSError during a
            # deploy) must not kill this thread — that would leave the bot
            # unsupervised forever with no further warning.
            print(f"[watchdog] Error supervising {bot_file}: {e}")
            returncode = None

        if _stop.is_set():
            break

        uptime = time.time() - start_time
        if uptime >= STABLE_UPTIME:
            delay = RESTART_DELAY  # ran long enough — reset backoff
        else:
            delay = min(delay * 2, MAX_RESTART_DELAY)  # crash loop — back off

        print(f"[watchdog] {bot_file} stopped after {uptime:.0f}s "
              f"(exit {returncode}). Restarting in {delay}s...")
        _stop.wait(timeout=delay)  # interruptible sleep — exits immediately on shutdown


_shutting_down = False

def _shutdown(signum, frame):
    """Clean shutdown: terminate all bot processes gracefully."""
    global _shutting_down
    if _shutting_down:
        # A second signal arrived while we're still shutting down (e.g. an
        # impatient double Ctrl+C) — ignore it instead of re-entering and
        # deadlocking on the non-reentrant _lock.
        return
    _shutting_down = True
    print("\n[watchdog] Shutting down...")
    _stop.set()
    with _lock:
        for bot_file, proc in _processes.items():
            try:
                proc.terminate()
                print(f"[watchdog] Terminated {bot_file}")
            except Exception:
                pass
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

print("Watchdog started. Monitoring all bots...")
print(f"Bot directory : {BOT_DIR}")
print(f"Log directory : {LOG_DIR}")
print(f"Python        : {PYTHON}")

# Sanity-check that discord.py is importable before spawning bots
import subprocess as _sp
_check = _sp.run([PYTHON, "-c", "import discord"], capture_output=True)
if _check.returncode != 0:
    print("ERROR: discord.py is not installed in the selected Python environment.")
    print(f"  Run: {PYTHON} -m pip install discord.py aiohttp")
    sys.exit(1)

threads = []
for bot in BOTS:
    t = threading.Thread(target=watch_bot, args=(bot,), daemon=True)
    t.start()
    threads.append(t)

_stop.wait()  # block main thread until shutdown signal
