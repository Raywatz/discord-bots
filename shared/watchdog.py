import subprocess
import time
import threading
import os
import sys
import signal
import json

# (subdirectory, script filename) — each bot lives in its own sibling
# directory next to shared/, not inside shared/ itself.
BOTS = [
    ("counting-bot", "counting_bot.py"),
    ("file-uploader-bot", "file_uploader_bot.py"),
    ("moderation-bot", "moderation_bot.py"),
    ("inbox-bot", "inbox_bot.py"),
    ("hub-bot", "hub_bot.py"),
    ("vibe-bot", "vibe_bot.py"),
    ("games-bot", "games_bot.py"),
    ("python-bot", "python_bot.py"),
    ("yt-music-bot", "yt_music_bot.py"),
]

RESTART_DELAY      = 3    # initial delay (seconds)
MAX_RESTART_DELAY  = 120  # cap backoff at 2 minutes
STABLE_UPTIME      = 60   # if bot runs > this many seconds, reset backoff
HEARTBEAT_CHECK_INTERVAL = 30   # how often to check heartbeat.json (seconds)
HEARTBEAT_STALE_AFTER    = 180  # kill+restart a bot whose heartbeat is older than this

# shared/ — this script's own directory, and the parent that holds every bot's subdirectory
SHARED_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(SHARED_DIR)
LOG_DIR    = os.path.join(SHARED_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Prefer the local venv Python (has discord.py installed) over the system Python
_venv_python = os.path.join(SHARED_DIR, ".venv", "bin", "python3")
PYTHON = _venv_python if os.path.exists(_venv_python) else sys.executable

_processes = {}  # bot_file -> subprocess.Popen
_lock = threading.Lock()
_stop = threading.Event()


def watch_bot(bot_subdir, bot_file):
    delay = RESTART_DELAY
    bot_dir = os.path.join(REPO_ROOT, bot_subdir)
    bot_path = os.path.join(bot_dir, bot_file)
    if not os.path.exists(bot_path):
        print(f"[watchdog] ERROR: {bot_path} does not exist — skipping {bot_file}.")
        return

    # bot_utils.py lives in shared/, not in each bot's own directory; put shared/
    # on PYTHONPATH so `import bot_utils` resolves regardless of the bot's cwd.
    env = os.environ.copy()
    env["PYTHONPATH"] = SHARED_DIR + os.pathsep + env.get("PYTHONPATH", "")

    while not _stop.is_set():
        start_time = time.time()
        try:
            log_path = os.path.join(LOG_DIR, bot_file.replace(".py", ".log"))
            print(f"[watchdog] Starting {bot_file} (log: logs/{bot_file.replace('.py', '.log')})...")

            with open(log_path, "a") as log:
                log.write(f"\n--- Started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                log.flush()
                process = subprocess.Popen(
                    [PYTHON, bot_path],
                    cwd=bot_dir,
                    env=env,
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


def watch_heartbeats():
    """A bot that's alive but hung (deadlocked, wedged on Discord's gateway)
    never exits on its own, so process.wait() in watch_bot never fires and
    it's never restarted. Poll heartbeat.json and kill any bot whose last
    heartbeat is stale — its own watch_bot loop then respawns it."""
    heartbeat_path = os.path.join(SHARED_DIR, "heartbeat.json")
    while not _stop.wait(timeout=HEARTBEAT_CHECK_INTERVAL):
        try:
            with open(heartbeat_path, "r") as f:
                heartbeats = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if not isinstance(heartbeats, dict):
            continue

        now = time.time()
        with _lock:
            live_procs = dict(_processes)
        for bot_file, process in live_procs.items():
            if process.poll() is not None:
                continue  # already exited — watch_bot will handle the restart
            bot_key = bot_file[:-3]  # e.g. "python_bot.py" -> "python_bot"; bots log under a short name too
            last_beat = None
            for name, ts in heartbeats.items():
                if bot_key == name or bot_key.startswith(name) or name in bot_key:
                    last_beat = ts if last_beat is None else max(last_beat, ts)
            if last_beat is None:
                continue  # this bot doesn't report heartbeats (e.g. yt_music_bot)
            if now - last_beat > HEARTBEAT_STALE_AFTER:
                print(f"[watchdog] {bot_file} heartbeat stale ({now - last_beat:.0f}s) — killing for restart.")
                try:
                    process.kill()
                except Exception:
                    pass


_shutting_down = False


def _shutdown(signum, frame):
    """Clean shutdown: terminate all bot processes and confirm they exit
    before this process does, falling back to kill() for stragglers."""
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
        procs = dict(_processes)
    for bot_file, proc in procs.items():
        try:
            proc.terminate()
        except Exception:
            pass
    for bot_file, proc in procs.items():
        try:
            proc.wait(timeout=10)
            print(f"[watchdog] Terminated {bot_file}")
        except subprocess.TimeoutExpired:
            print(f"[watchdog] {bot_file} didn't exit in time — killing.")
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        except Exception:
            pass
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

print("Watchdog started. Monitoring all bots...")
print(f"Repo root     : {REPO_ROOT}")
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
for bot_subdir, bot in BOTS:
    t = threading.Thread(target=watch_bot, args=(bot_subdir, bot), daemon=True)
    t.start()
    threads.append(t)

hb_thread = threading.Thread(target=watch_heartbeats, daemon=True)
hb_thread.start()

_stop.wait()  # block main thread until shutdown signal
