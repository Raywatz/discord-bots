import subprocess
import time
import threading
import os
import sys
import signal

# Map each bot script to the sibling directory (relative to the repo root)
# it actually lives in. Bot scripts do NOT live next to this watchdog.
BOTS = {
    "counting_bot.py": "counting-bot",
    "file_uploader_bot.py": "file-uploader-bot",
    "moderation_bot.py": "moderation-bot",
    "inbox_bot.py": "inbox-bot",
    "hub_bot.py": "hub-bot",
    "vibe_bot.py": "vibe-bot",
    "games_bot.py": "games-bot",
    "python_bot.py": "python-bot",
    "yt_music_bot.py": "yt-music-bot",
}

RESTART_DELAY     = 3    # initial delay (seconds)
MAX_RESTART_DELAY = 120  # cap backoff at 2 minutes
STABLE_UPTIME     = 60   # if bot runs > this many seconds, reset backoff

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BOT_DIR)  # parent of shared/, where each bot's own dir lives
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
    bot_dir = os.path.join(REPO_ROOT, BOTS[bot_file])
    # bot_utils.py lives in shared/, so bots that "import bot_utils" need it on PYTHONPATH
    bot_env = dict(os.environ)
    bot_env["PYTHONPATH"] = BOT_DIR + os.pathsep + bot_env.get("PYTHONPATH", "")
    while not _stop.is_set():
        log_path = os.path.join(LOG_DIR, bot_file.replace(".py", ".log"))
        print(f"[watchdog] Starting {bot_file} (log: logs/{bot_file.replace('.py', '.log')})...")
        start_time = time.time()

        process = None
        try:
            with open(log_path, "a") as log:
                log.write(f"\n--- Started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                log.flush()
                process = subprocess.Popen(
                    [PYTHON, bot_file],
                    cwd=bot_dir,
                    stdout=log,
                    stderr=log,
                    env=bot_env
                )
                with _lock:
                    _processes[bot_file] = process
                process.wait()
        except Exception as e:
            # Don't let a Popen/log-file failure permanently kill this bot's
            # supervisor thread — log it and fall through to the backoff/retry.
            print(f"[watchdog] ERROR supervising {bot_file}: {e}")

        if _stop.is_set():
            break

        uptime = time.time() - start_time
        if uptime >= STABLE_UPTIME:
            delay = RESTART_DELAY  # ran long enough — reset backoff
        else:
            delay = min(delay * 2, MAX_RESTART_DELAY)  # crash loop — back off

        returncode = process.returncode if process is not None else "n/a"
        print(f"[watchdog] {bot_file} stopped after {uptime:.0f}s "
              f"(exit {returncode}). Restarting in {delay}s...")
        _stop.wait(timeout=delay)  # interruptible sleep — exits immediately on shutdown


def _shutdown(signum, frame):
    """Clean shutdown: terminate all bot processes gracefully."""
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
