import discord
from discord import app_commands
from discord.ext import tasks
import json
import os
import asyncio
import bot_utils

# CONFIG
TOKEN            = os.environ.get("DISCORD_COUNTING_BOT_TOKEN", "")
CORRECT_EMOJI    = "✅"
WRONG_EMOJI      = "❌"

MODE1_SAVE       = "mode1_save.txt"
MODE2_SAVE       = "mode2_save.txt"
MODE3_SAVE       = "mode3_save.txt"
HIGH_SCORE_FILE  = "high_score.txt"
MODE_FILE        = "mode_state.txt"
LEADERBOARD_FILE = "leaderboard.json"
HS_ANNOUNCED_FILE= "hs_announced.json"
SETUP_FILE       = "counting_setup.json"
DISABLE_FILE     = "disable_data.json"
HUB_FILE         = "hub_data.json"
BLOCKED_FILE     = "counting_blocked.json"  # for 10s block via mod link

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree   = app_commands.CommandTree(client)


def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


mode1_data    = load_json(MODE1_SAVE)
mode2_data    = load_json(MODE2_SAVE)
mode3_data    = load_json(MODE3_SAVE)
high_scores   = load_json(HIGH_SCORE_FILE)
mode_state    = load_json(MODE_FILE)
leaderboard   = load_json(LEADERBOARD_FILE)
hs_announced  = load_json(HS_ANNOUNCED_FILE)
setup_data    = load_json(SETUP_FILE)

FIBONACCI = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377, 610,
             987, 1597, 2584, 4181, 6765, 10946, 17711, 28657, 46368,
             75025, 121393, 196418, 317811, 514229, 832040]

def fib_expected(step):
    if step < len(FIBONACCI):
        return FIBONACCI[step]
    a, b = FIBONACCI[-2], FIBONACCI[-1]
    for _ in range(step - len(FIBONACCI) + 1):
        a, b = b, a + b
    return b

def get_counting_channel(guild_id):
    return load_json(SETUP_FILE).get(guild_id, {}).get("channel", "counting")

def get_mode(guild_id):
    return mode_state.get(guild_id, "mode1")

def get_high_score(guild_id, mode):
    return high_scores.get(guild_id, {}).get(mode, 0)

def set_high_score(guild_id, mode, score):
    if guild_id not in high_scores:
        high_scores[guild_id] = {}
    high_scores[guild_id][mode] = score
    save_json(HIGH_SCORE_FILE, high_scores)

def get_count(data, guild_id):
    entry = data.get(guild_id, {})
    if not isinstance(entry, dict):
        return 0
    return entry.get("count", entry.get("step", 0))

def get_last_counter(data, guild_id):
    entry = data.get(guild_id, {})
    if not isinstance(entry, dict):
        return None
    return entry.get("last_counter", None)

def set_state(data, path, guild_id, count, last_counter_id):
    if guild_id not in data:
        data[guild_id] = {}
    data[guild_id]["count"]        = count
    data[guild_id]["last_counter"] = last_counter_id
    save_json(path, data)

def mode2_expected(step):
    return 2 ** step

def get_mode_label(mode):
    return {"mode1": "Mode 1 (1, 2, 3...)", "mode2": "Mode 2 (1, 2, 4, 8...)", "mode3": "Mode 3 (Fibonacci)"}.get(mode, mode)

def is_bot_disabled(guild_id):
    return load_json(DISABLE_FILE).get(str(guild_id), {}).get("counting", False)

def is_mode_enabled(guild_id, mode):
    return load_json(HUB_FILE).get(str(guild_id), {}).get("counting_modes", {}).get(mode, True)

def is_blocked(guild_id, user_id):
    import time
    blocked = load_json(BLOCKED_FILE)
    entry   = blocked.get(guild_id, {}).get(str(user_id))
    if entry and time.time() < entry:
        return True
    return False

def add_to_leaderboard(guild_id, user_id, username):
    if guild_id not in leaderboard:
        leaderboard[guild_id] = {}
    uid = str(user_id)
    if uid not in leaderboard[guild_id]:
        leaderboard[guild_id][uid] = {"name": username, "count": 0}
    leaderboard[guild_id][uid]["count"] += 1
    leaderboard[guild_id][uid]["name"]   = username
    save_json(LEADERBOARD_FILE, leaderboard)

def handle_high_score(guild_id, mode, number):
    prev_best = get_high_score(guild_id, mode)
    key       = f"{guild_id}:{mode}"
    if number > prev_best:
        set_high_score(guild_id, mode, number)
        if not hs_announced.get(key, False):
            hs_announced[key] = True
            save_json(HS_ANNOUNCED_FILE, hs_announced)
            return f"New high score! **{number}** and counting!"
    return None

def build_fail_msg(guild_id, mode, current_val, mention):
    prev_best = get_high_score(guild_id, mode)
    if current_val > prev_best:
        set_high_score(guild_id, mode, current_val)
        record_msg = f" New high score of **{current_val}**!"
    else:
        record_msg = f" High score is still **{prev_best}**."
    key = f"{guild_id}:{mode}"
    hs_announced[key] = False
    save_json(HS_ANNOUNCED_FILE, hs_announced)
    return f"{mention} broke the count at **{current_val}**!{record_msg} Starting over — type **1** to begin!"


# ── /setup ────────────────────────────────────────────────────────────────────
class CountingSetupView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(label=ch.name, value=ch.name)
            for ch in guild.text_channels[:25]
        ]
        select = discord.ui.Select(placeholder="Choose the counting channel", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        channel  = interaction.data["values"][0]
        fresh = load_json(SETUP_FILE)
        if guild_id not in fresh:
            fresh[guild_id] = {}
        fresh[guild_id]["channel"] = channel
        save_json(SETUP_FILE, fresh)
        setup_data.clear()
        setup_data.update(fresh)
        await interaction.response.send_message(f"Counting channel set to **#{channel}**.", ephemeral=True)
        self.stop()

@tree.command(name="setup", description="Set the counting channel for this server")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    view = CountingSetupView(interaction.guild)
    await interaction.response.send_message("Select the counting channel:", view=view, ephemeral=True)


# ── /reset ────────────────────────────────────────────────────────────────────
@tree.command(name="reset", description="Reset the count to 0")
@app_commands.default_permissions(administrator=True)
async def reset(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    mode     = get_mode(guild_id)
    if mode == "mode1":
        set_state(mode1_data, MODE1_SAVE, guild_id, 0, None)
    elif mode == "mode2":
        set_state(mode2_data, MODE2_SAVE, guild_id, 0, None)
    elif mode == "mode3":
        set_state(mode3_data, MODE3_SAVE, guild_id, 0, None)
    await interaction.response.send_message("Count has been reset to 0.", ephemeral=True)


# ── /leaderboard ──────────────────────────────────────────────────────────────
@tree.command(name="leaderboard", description="Show the top counters for this server")
async def leaderboard_cmd(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Counting bot is disabled.", ephemeral=True)
        return
    board = leaderboard.get(guild_id, {})
    if not board:
        await interaction.response.send_message("No counting data yet!")
        return
    sorted_board = sorted(board.items(), key=lambda x: x[1]["count"], reverse=True)[:10]
    lines = [f"**#{i+1}** {entry['name']} — {entry['count']} correct counts"
             for i, (_, entry) in enumerate(sorted_board)]
    msg = "**Counting Leaderboard:**\n" + "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…(truncated)"
    await interaction.response.send_message(msg)


# ── /mode1 /mode2 /mode3 ──────────────────────────────────────────────────────
@tree.command(name="mode1", description="Switch to Mode 1: count 1, 2, 3, 4...")
@app_commands.default_permissions(administrator=True)
async def mode1(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if not is_mode_enabled(guild_id, "mode1"):
        await interaction.response.send_message("Mode 1 is disabled.", ephemeral=True)
        return
    mode_state[guild_id] = "mode1"
    save_json(MODE_FILE, mode_state)
    current = get_count(mode1_data, guild_id)
    await interaction.response.send_message(f"Switched to **Mode 1**. Current count: **{current}**.", ephemeral=True)


@tree.command(name="mode2", description="Switch to Mode 2: count 1, 2, 4, 8, 16...")
@app_commands.default_permissions(administrator=True)
async def mode2(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if not is_mode_enabled(guild_id, "mode2"):
        await interaction.response.send_message("Mode 2 is disabled.", ephemeral=True)
        return
    mode_state[guild_id] = "mode2"
    save_json(MODE_FILE, mode_state)
    step    = get_count(mode2_data, guild_id)
    current = mode2_expected(step - 1) if step > 0 else 0
    await interaction.response.send_message(f"Switched to **Mode 2**. Current number: **{current}**.", ephemeral=True)


@tree.command(name="mode3", description="Switch to Mode 3: Fibonacci")
@app_commands.default_permissions(administrator=True)
async def mode3(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if not is_mode_enabled(guild_id, "mode3"):
        await interaction.response.send_message("Mode 3 is disabled.", ephemeral=True)
        return
    mode_state[guild_id] = "mode3"
    save_json(MODE_FILE, mode_state)
    step    = get_count(mode3_data, guild_id)
    current = fib_expected(step - 1) if step > 0 else 0
    await interaction.response.send_message(f"Switched to **Mode 3** (Fibonacci). Current number: **{current}**.", ephemeral=True)


# ── /highscore /num /w ────────────────────────────────────────────────────────
@tree.command(name="highscore", description="Show the high score for the current mode")
async def highscore(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Counting bot is disabled.", ephemeral=True)
        return
    mode  = get_mode(guild_id)
    score = get_high_score(guild_id, mode)
    if score == 0:
        await interaction.response.send_message(f"No high score yet for **{get_mode_label(mode)}**!")
    else:
        await interaction.response.send_message(f"High score for **{get_mode_label(mode)}**: **{score}**")


@tree.command(name="num", description="Show the current number")
async def num(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Counting bot is disabled.", ephemeral=True)
        return
    mode = get_mode(guild_id)
    if mode == "mode1":
        current = get_count(mode1_data, guild_id)
        await interaction.response.send_message(f"Current number in **Mode 1**: **{current}**.")
    elif mode == "mode2":
        step    = get_count(mode2_data, guild_id)
        current = mode2_expected(step - 1) if step > 0 else 0
        await interaction.response.send_message(f"Current number in **Mode 2**: **{current}**.")
    elif mode == "mode3":
        step = get_count(mode3_data, guild_id)
        if step == 0:
            await interaction.response.send_message("No numbers counted yet in **Mode 3**.")
        elif step == 1:
            await interaction.response.send_message(f"Last number in **Mode 3**: **{fib_expected(0)}**.")
        else:
            prev = fib_expected(step - 2)
            curr = fib_expected(step - 1)
            await interaction.response.send_message(f"Last 2 numbers in **Mode 3**: **{prev}**, **{curr}**.")


@tree.command(name="w", description="W")
async def w(interaction: discord.Interaction):
    await interaction.response.send_message("https://tenor.com/view/rock-moai-dwayne-johnson-d3s-gif-17850471898251413922")


# ── /stats ────────────────────────────────────────────────────────────────────
@tree.command(name="stats", description="Show counting stats for yourself or another user")
@app_commands.describe(user="User to check (defaults to you)")
async def stats(interaction: discord.Interaction, user: discord.Member = None):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Counting bot is disabled.", ephemeral=True)
        return
    target = user or interaction.user
    uid    = str(target.id)
    board  = leaderboard.get(guild_id, {})
    entry  = board.get(uid)
    if not entry:
        name = target.display_name
        await interaction.response.send_message(
            f"**{name}** has no counting contributions yet.", ephemeral=True
        )
        return
    sorted_board = sorted(board.items(), key=lambda x: x[1].get("count", 0), reverse=True)
    rank = next((i + 1 for i, (k, _) in enumerate(sorted_board) if k == uid), "?")
    mode = get_mode(guild_id)
    mode_labels = {"mode1": "Mode 1 (Sequential)", "mode2": "Mode 2 (Powers of 2)", "mode3": "Mode 3 (Fibonacci)"}
    await interaction.response.send_message(
        f"**Stats for {target.display_name}**\n"
        f"Total correct counts: **{entry.get('count', 0)}**\n"
        f"Server rank: **#{rank}**\n"
        f"Active mode: **{mode_labels.get(mode, mode)}**"
    )


# ── /streak ───────────────────────────────────────────────────────────────────
@tree.command(name="streak", description="Show the current counting streak for this server")
async def streak(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if is_bot_disabled(guild_id):
        await interaction.response.send_message("Counting bot is disabled.", ephemeral=True)
        return
    mode   = get_mode(guild_id)
    hs_all = high_scores.get(guild_id, {})
    if mode == "mode1":
        current = get_count(mode1_data, guild_id)
        hs      = hs_all.get("mode1", 0)
        label   = "Sequential"
    elif mode == "mode2":
        step    = get_count(mode2_data, guild_id)
        current = mode2_expected(step - 1) if step > 0 else 0
        hs      = hs_all.get("mode2", 0)
        label   = "Powers of 2"
    else:
        step    = get_count(mode3_data, guild_id)
        current = fib_expected(step - 1) if step > 0 else 0
        hs      = hs_all.get("mode3", 0)
        label   = "Fibonacci"
    pct = f" ({current/hs*100:.0f}% of record)" if hs > 0 else ""
    await interaction.response.send_message(
        f"**Current Streak — {label}**\n"
        f"Current: **{current}**{pct}\n"
        f"All-time high: **{hs}**"
    )


# ── /countpause ───────────────────────────────────────────────────────────────
@tree.command(name="countpause", description="Temporarily pause the counting channel")
@app_commands.default_permissions(administrator=True)
async def countpause(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    setup = load_json(SETUP_FILE)
    if guild_id not in setup:
        await interaction.response.send_message("Counting not set up yet.", ephemeral=True)
        return
    setup[guild_id]["paused"] = True
    save_json(SETUP_FILE, setup)
    await interaction.response.send_message("⏸ Counting channel paused. Use `/countresume` to unpause.", ephemeral=True)


# ── /countresume ──────────────────────────────────────────────────────────────
@tree.command(name="countresume", description="Resume the counting channel")
@app_commands.default_permissions(administrator=True)
async def countresume(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    setup = load_json(SETUP_FILE)
    if guild_id not in setup:
        await interaction.response.send_message("Counting not set up yet.", ephemeral=True)
        return
    setup[guild_id]["paused"] = False
    save_json(SETUP_FILE, setup)
    await interaction.response.send_message("▶️ Counting channel resumed.", ephemeral=True)


# ── /exclude ──────────────────────────────────────────────────────────────────
@tree.command(name="exclude", description="Prevent a user from counting")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="User to exclude from counting")
async def exclude(interaction: discord.Interaction, user: discord.Member):
    guild_id = str(interaction.guild_id)
    setup = load_json(SETUP_FILE)
    if guild_id not in setup:
        await interaction.response.send_message("Counting not set up yet.", ephemeral=True)
        return
    excluded = setup[guild_id].get("excluded", [])
    if user.id in excluded:
        await interaction.response.send_message(f"{user.mention} is already excluded.", ephemeral=True)
        return
    excluded.append(user.id)
    setup[guild_id]["excluded"] = excluded
    save_json(SETUP_FILE, setup)
    await interaction.response.send_message(f"🚫 {user.mention} excluded from counting.", ephemeral=True)


# ── /include ──────────────────────────────────────────────────────────────────
@tree.command(name="include", description="Re-allow a previously excluded user to count")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="User to re-allow")
async def include(interaction: discord.Interaction, user: discord.Member):
    guild_id = str(interaction.guild_id)
    setup = load_json(SETUP_FILE)
    if guild_id not in setup:
        await interaction.response.send_message("Counting not set up yet.", ephemeral=True)
        return
    excluded = setup[guild_id].get("excluded", [])
    if user.id not in excluded:
        await interaction.response.send_message(f"{user.mention} is not excluded.", ephemeral=True)
        return
    excluded.remove(user.id)
    setup[guild_id]["excluded"] = excluded
    save_json(SETUP_FILE, setup)
    await interaction.response.send_message(f"✅ {user.mention} can count again.", ephemeral=True)


@tasks.loop(seconds=60)
async def heartbeat_task():
    bot_utils.write_heartbeat("counting")

@heartbeat_task.before_loop
async def before_heartbeat():
    await client.wait_until_ready()


@client.event
async def on_error(event_method, *args, **kwargs):
    import traceback, sys
    exc = sys.exc_info()[1]
    guild_id = None
    if args and hasattr(args[0], 'guild') and args[0].guild:
        guild_id = str(args[0].guild.id)
    if isinstance(exc, discord.HTTPException) and exc.status == 429:
        bot_utils.log_event("counting", "rate_limit",
            f"Rate limited in {event_method}: {exc}", guild_id=guild_id)
    elif exc:
        bot_utils.log_event("counting", "error",
            f"Unhandled error in {event_method}: {traceback.format_exc()[:500]}",
            guild_id=guild_id)

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if isinstance(error, app_commands.CommandInvokeError):
        inner = error.original
        if isinstance(inner, discord.HTTPException) and inner.status == 429:
            bot_utils.log_event("counting", "rate_limit",
                f"Rate limited on /{interaction.command.name if interaction.command else '?'}: {inner}",
                guild_id=guild_id)
            if not interaction.response.is_done():
                await interaction.response.send_message("Bot is being rate limited. Try again in a moment.", ephemeral=True)
            return
    bot_utils.log_event("counting", "error",
        f"Command error on /{interaction.command.name if interaction.command else '?'}: {error}",
        guild_id=guild_id)
    if not interaction.response.is_done():
        await interaction.response.send_message("An error occurred.", ephemeral=True)


@client.event
async def on_ready():
    await tree.sync()
    if not heartbeat_task.is_running():
        heartbeat_task.start()
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    guild_id = str(message.guild.id) if message.guild else None
    if not guild_id:
        return

    counting_channel = get_counting_channel(guild_id)
    if message.channel.name != counting_channel:
        return
    if not message.content.strip().isdecimal():
        return
    if is_bot_disabled(guild_id):
        return
    setup_data_fresh = load_json(SETUP_FILE)
    guild_setup = setup_data_fresh.get(str(message.guild.id), {})
    if guild_setup.get("paused", False):
        return
    excluded = guild_setup.get("excluded", [])
    if message.author.id in excluded:
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        return
    if is_blocked(guild_id, message.author.id):
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        return

    number = int(message.content.strip())
    mode   = get_mode(guild_id)

    if not is_mode_enabled(guild_id, mode):
        return

    if mode == "mode1":
        current_count   = get_count(mode1_data, guild_id)
        last_counter_id = get_last_counter(mode1_data, guild_id)
        expected        = current_count + 1
        if message.author.id == last_counter_id:
            await message.channel.send(f"{message.author.mention} you can't count twice in a row!")
            return
        if number == expected:
            set_state(mode1_data, MODE1_SAVE, guild_id, number, message.author.id)
            add_to_leaderboard(guild_id, message.author.id, message.author.name)
            await message.add_reaction(CORRECT_EMOJI)
            msg = handle_high_score(guild_id, "mode1", number)
            if msg:
                await message.channel.send(msg)
        else:
            await message.add_reaction(WRONG_EMOJI)
            try:
                await message.pin()
            except (discord.Forbidden, discord.HTTPException):
                pass
            await message.reply(build_fail_msg(guild_id, "mode1", current_count, message.author.mention))
            set_state(mode1_data, MODE1_SAVE, guild_id, 0, None)

    elif mode == "mode2":
        step            = get_count(mode2_data, guild_id)
        last_counter_id = get_last_counter(mode2_data, guild_id)
        expected        = mode2_expected(step)
        if message.author.id == last_counter_id:
            await message.channel.send(f"{message.author.mention} you can't count twice in a row!")
            return
        if number == expected:
            set_state(mode2_data, MODE2_SAVE, guild_id, step + 1, message.author.id)
            add_to_leaderboard(guild_id, message.author.id, message.author.name)
            await message.add_reaction(CORRECT_EMOJI)
            msg = handle_high_score(guild_id, "mode2", number)
            if msg:
                await message.channel.send(msg)
        else:
            await message.add_reaction(WRONG_EMOJI)
            try:
                await message.pin()
            except (discord.Forbidden, discord.HTTPException):
                pass
            current_val = mode2_expected(step - 1) if step > 0 else 0
            await message.reply(build_fail_msg(guild_id, "mode2", current_val, message.author.mention))
            set_state(mode2_data, MODE2_SAVE, guild_id, 0, None)

    elif mode == "mode3":
        step            = get_count(mode3_data, guild_id)
        last_counter_id = get_last_counter(mode3_data, guild_id)
        expected        = fib_expected(step)
        if message.author.id == last_counter_id:
            await message.channel.send(f"{message.author.mention} you can't count twice in a row!")
            return
        if number == expected:
            set_state(mode3_data, MODE3_SAVE, guild_id, step + 1, message.author.id)
            add_to_leaderboard(guild_id, message.author.id, message.author.name)
            await message.add_reaction(CORRECT_EMOJI)
            msg = handle_high_score(guild_id, "mode3", number)
            if msg:
                await message.channel.send(msg)
        else:
            await message.add_reaction(WRONG_EMOJI)
            try:
                await message.pin()
            except (discord.Forbidden, discord.HTTPException):
                pass
            current_val = fib_expected(step - 1) if step > 0 else 0
            await message.reply(build_fail_msg(guild_id, "mode3", current_val, message.author.mention))
            set_state(mode3_data, MODE3_SAVE, guild_id, 0, None)


client.run(TOKEN)
