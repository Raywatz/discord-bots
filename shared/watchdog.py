import subprocess
import time
import threading
import os
import sys
import signal

# bot script filename -> the sibling directory it actually lives in
BOT_DIRS = {
    "counting_bot.py":      "counting-bot",
    "file_uploader_bot.py": "file-uploader-bot",
    "moderation_bot.py":    "moderation-bot",
    "inbox_bot.py":         "inbox-bot",
    "hub_bot.py":           "hub-bot",
    "vibe_bot.py":          "vibe-bot",
    "games_bot.py":         "games-bot",
    "python_bot.py":        "python-bot",
    "yt_music_bot.py":      "yt-music-bot",
}
BOTS = list(BOT_DIRS.keys())

RESTART_DELAY     = 3    # initial delay (seconds)
MAX_RESTART_DELAY = 120  # cap backoff at 2 minutes
STABLE_UPTIME     = 60   # if bot runs > this many seconds, reset backoff

# shared/ (this script's own directory) and the repo root (its parent, which
# contains each bot's own subdirectory, e.g. counting-bot/counting_bot.py)
BOT_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BOT_DIR)
LOG_DIR  = os.path.join(BOT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Prefer the local venv Python (has discord.py installed) over the system Python
_venv_python = os.path.join(BOT_DIR, ".venv", "bin", "python3")
PYTHON = _venv_python if os.path.exists(_venv_python) else sys.executable

# Each bot does `import bot_utils`, which lives in shared/ (this directory) —
# put it on PYTHONPATH so that import resolves regardless of the bot's own cwd.
_BOT_ENV = os.environ.copy()
_BOT_ENV["PYTHONPATH"] = BOT_DIR + os.pathsep + _BOT_ENV.get("PYTHONPATH", "")

_processes = {}  # bot_file -> subprocess.Popen
_lock = threading.Lock()
_stop = threading.Event()


def watch_bot(bot_file):
    delay = RESTART_DELAY
    bot_dir = os.path.join(REPO_DIR, BOT_DIRS[bot_file])
    while not _stop.is_set():
        log_path = os.path.join(LOG_DIR, bot_file.replace(".py", ".log"))
        print(f"[watchdog] Starting {bot_file} (log: logs/{bot_file.replace('.py', '.log')})...")
        start_time = time.time()

        try:
            with open(log_path, "a") as log:
                log.write(f"\n--- Started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                log.flush()
                process = subprocess.Popen(
                    [PYTHON, bot_file],
                    cwd=bot_dir,
                    env=_BOT_ENV,
                    stdout=log,
                    stderr=log
                )
                with _lock:
                    _processes[bot_file] = process
                process.wait()

            if _stop.is_set():
                break

            uptime = time.time() - start_time
            if uptime >= STABLE_UPTIME:
                delay = RESTART_DELAY  # ran long enough — reset backoff
            else:
                delay = min(delay * 2, MAX_RESTART_DELAY)  # crash loop — back off

            print(f"[watchdog] {bot_file} stopped after {uptime:.0f}s "
                  f"(exit {process.returncode}). Restarting in {delay}s...")
        except Exception as e:
            delay = min(delay * 2, MAX_RESTART_DELAY)
            print(f"[watchdog] Error supervising {bot_file}: {e}. Retrying in {delay}s...")
            _stop.wait(timeout=delay)
            continue

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
