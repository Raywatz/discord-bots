import discord
from discord import app_commands
from discord.ext import tasks
import json
import os
import sys
import subprocess
import tempfile
import asyncio
import time
import re
import bot_utils

TOKEN        = os.environ.get("DISCORD_PYTHON_BOT_TOKEN", "")
HUB_FILE     = "hub_data.json"
DISABLE_FILE = "disable_data.json"
PYTHON_LOG   = "python_log.json"

# Imports blocked in non-sudo mode
BLOCKED_IMPORTS = {
    "os", "sys", "subprocess", "shutil", "socket", "requests", "urllib",
    "http", "ftplib", "smtplib", "paramiko", "pexpect", "pty",
    "ctypes", "cffi", "pickle", "shelve", "marshal",
}

BLOCKED_PATTERNS = [
    r"\bopen\s*\(",        # open() file access
    r"\b__import__\s*\(",  # dynamic import
    r"\bcompile\s*\(",     # compile()
    r"\bgetattr\s*\(",     # attribute access by string
    r"\bsetattr\s*\(",
    r"\bdelattr\s*\(",
    r"\bglobals\s*\(",
    r"\blocals\s*\(",
    r"\bvars\s*\(",
    r"\beval\s*\(",
    r"\bexec\s*\(",
]

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON  = os.path.join(BOT_DIR, ".venv", "bin", "python3")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        try:   return json.load(f)
        except Exception: return {}

def log_run(guild_id: str, user_id: str, user_name: str, code: str,
            stdout: str, stderr: str, sudo: bool) -> None:
    import uuid as _uuid
    path = os.path.join(BOT_DIR, PYTHON_LOG)
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception:
        data = []
    if not isinstance(data, list):
        data = []
    data.insert(0, {
        "id":        str(_uuid.uuid4()),
        "guild_id":  guild_id,
        "user_id":   user_id,
        "user_name": user_name,
        "code":      code,
        "stdout":    stdout,
        "stderr":    stderr,
        "sudo":      sudo,
        "timestamp": time.time(),
    })
    data = data[:500]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def is_bot_disabled(guild_id: str) -> bool:
    data = load_json(DISABLE_FILE)
    return data.get(guild_id, {}).get("python", False)

def get_hub(guild_id: str) -> dict:
    data = load_json(HUB_FILE)
    return data.get(guild_id, {})

def is_scripts_public(guild_id: str) -> bool:
    return get_hub(guild_id).get("scripts_public", False)

def is_sudo_enabled(guild_id: str) -> bool:
    return get_hub(guild_id).get("sudo_enabled", False)

def is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    return interaction.user.guild_permissions.administrator


def check_code_safety(code: str) -> str | None:
    """
    Returns an error message if the code contains blocked patterns,
    or None if it's safe to run.
    """
    for line in code.splitlines():
        stripped = line.strip()
        # Check import statements
        m = re.match(r"^(?:import|from)\s+(\w+)", stripped)
        if m:
            mod = m.group(1)
            if mod in BLOCKED_IMPORTS:
                return f"Import of `{mod}` is not allowed in restricted mode. Ask an admin to enable sudo."
    # Check dangerous built-in patterns
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, code):
            nice = pattern.replace(r"\b", "").replace(r"\s*\(", "()").replace("\\", "")
            return f"Pattern `{nice}` is not allowed in restricted mode."
    return None


def needs_input(code: str) -> bool:
    """True if the code contains any input() calls."""
    return bool(re.search(r'\binput\s*\(', code))


async def execute_code(code: str, timeout: int = 8, stdin_data: str = "") -> tuple[str, str]:
    """
    Run Python code in a subprocess. Returns (stdout, stderr).
    Timeout in seconds. Both streams are capped at 3000 chars.
    stdin_data is fed line-by-line to any input() calls.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir=BOT_DIR) as f:
        f.write(code)
        tmp_path = f.name

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [PYTHON, tmp_path],
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tempfile.gettempdir(),  # run in /tmp, not bot dir
            )
        )
        stdout = result.stdout[:3000]
        stderr = result.stderr[:3000]
        return stdout, stderr
    except subprocess.TimeoutExpired:
        return "", f"⏱ Code timed out after {timeout} seconds."
    except Exception as e:
        return "", f"Execution error: {e}"
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def format_output(stdout: str, stderr: str, code: str, stdin_data: str = "") -> str:
    lines = [f"```py\n{code[:500]}{'…' if len(code)>500 else ''}\n```"]
    if stdin_data.strip():
        # Show inputs interleaved: stdout already contains prompts, just annotate the values
        input_lines = stdin_data.strip().splitlines()
        annotated = "\n".join(f"→ {l}" for l in input_lines)
        lines.append(f"**Inputs provided:**\n```\n{annotated}\n```")
    if stdout:
        out = stdout if len(stdout) <= 1500 else stdout[:1500] + "\n…(truncated)"
        lines.append(f"**Output:**\n```\n{out}\n```")
    if stderr:
        err = stderr if len(stderr) <= 800 else stderr[:800] + "\n…(truncated)"
        lines.append(f"**Errors:**\n```\n{err}\n```")
    if not stdout and not stderr:
        lines.append("*(no output)*")
    return "\n".join(lines)


# ── Input helpers ─────────────────────────────────────────────────────────────

def extract_input_prompts(code: str) -> list:
    """Return list of prompt strings from every input() call in the code."""
    prompts = []
    for m in re.finditer(r'\binput\s*\(([^)]*)\)', code):
        arg = m.group(1).strip()
        if not arg:
            prompts.append("(no prompt)")
        else:
            inner = re.match(r'^[fF]?["\'](.+?)["\']$', arg)
            prompts.append(inner.group(1) if inner else arg)
    return prompts

def fmt_prompts(prompts: list) -> str:
    if not prompts:
        return "*(no prompts detected)*"
    return "\n".join(f"**{i+1}.** {p}" for i, p in enumerate(prompts))


# ── Shared: run code and return formatted output string ───────────────────────

async def run_and_format(code: str, guild_id: str, sudo: bool,
                         stdin_data: str, user: discord.User) -> str:
    stdout, stderr = await execute_code(code, stdin_data=stdin_data)
    output = format_output(stdout, stderr, code, stdin_data)
    if len(output) > 2000:
        output = output[:1997] + "…"
    log_run(guild_id=guild_id, user_id=str(user.id), user_name=str(user.display_name),
            code=code, stdout=stdout, stderr=stderr, sudo=sudo)
    return output


# ── InputModal — used in all three paths ──────────────────────────────────────

class InputModal(discord.ui.Modal, title="Provide Inputs"):
    inputs = discord.ui.TextInput(
        label="Input values (one per line, in order)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=2000,
    )

    def __init__(self, code: str, guild_id: str, sudo: bool,
                 prompts: list = None, post_to_channel_id: int = None):
        super().__init__()
        self.code_str           = code
        self.guild_id           = guild_id
        self.sudo               = sudo
        self.post_to_channel_id = post_to_channel_id  # set for DM path
        # Placeholder shows what each input() is asking for
        if prompts:
            ph = "  |  ".join(f"{i+1}. {p}" for i, p in enumerate(prompts))
            self.inputs.placeholder = ph[:100]
        else:
            self.inputs.placeholder = "Enter each value on its own line, in order"

    async def on_submit(self, interaction: discord.Interaction):
        stdin_data = self.inputs.value or ""
        if self.post_to_channel_id:
            # Called from a DM — acknowledge privately, post output to original channel
            await interaction.response.defer(ephemeral=True)
            output = await run_and_format(self.code_str, self.guild_id, self.sudo,
                                          stdin_data, interaction.user)
            ch = client.get_channel(self.post_to_channel_id)
            posted = False
            if ch:
                try:
                    await ch.send(output)
                    posted = True
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    posted = False
            if posted:
                await interaction.followup.send("✅ Inputs submitted! Output posted in the channel.",
                                                ephemeral=True)
            else:
                prefix = ("✅ Inputs submitted, but I couldn't post the output in the "
                          "original channel. Here it is:\n")
                fallback = prefix + output
                if len(fallback) > 2000:
                    fallback = fallback[:1997] + "…"
                await interaction.followup.send(fallback, ephemeral=True)
        else:
            await interaction.response.defer()
            output = await run_and_format(self.code_str, self.guild_id, self.sudo,
                                          stdin_data, interaction.user)
            await interaction.followup.send(output, ephemeral=False)


# ── InputMethodView — 3-button picker shown after code submission ─────────────

class InputMethodView(discord.ui.View):
    def __init__(self, code: str, guild_id: str, sudo: bool,
                 prompts: list, channel_id: int):
        super().__init__(timeout=300)
        self.code_str   = code
        self.guild_id   = guild_id
        self.sudo       = sudo
        self.prompts    = prompts
        self.channel_id = channel_id

    @discord.ui.button(label="Fill in myself", style=discord.ButtonStyle.primary, emoji="✏️")
    async def self_input(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            InputModal(self.code_str, self.guild_id, self.sudo, prompts=self.prompts)
        )

    @discord.ui.button(label="Assign to someone", style=discord.ButtonStyle.secondary, emoji="👤")
    async def assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            AssignInputModal(self.code_str, self.guild_id, self.sudo,
                             self.prompts, self.channel_id)
        )

    @discord.ui.button(label="Public request", style=discord.ButtonStyle.success, emoji="🌐")
    async def public_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        prompt_str = fmt_prompts(self.prompts)
        view = PublicInputView(self.code_str, self.guild_id, self.sudo, self.prompts)
        await interaction.response.send_message(
            f"🐍 **Input needed for a Python script**\n"
            f"**Inputs required:**\n{prompt_str}\n\n"
            f"Anyone can click below to provide values and run the code:",
            view=view,
        )
        self.stop()


# ── AssignInputModal — pick who fills in the inputs ───────────────────────────

class AssignInputModal(discord.ui.Modal, title="Assign Inputs to Someone"):
    mention = discord.ui.TextInput(
        label="Who should fill in the inputs?",
        placeholder="@mention them (e.g. @Username)",
        required=True,
        max_length=100,
    )

    def __init__(self, code: str, guild_id: str, sudo: bool,
                 prompts: list, channel_id: int):
        super().__init__()
        self.code_str   = code
        self.guild_id   = guild_id
        self.sudo       = sudo
        self.prompts    = prompts
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        m = re.search(r'<@!?(\d+)>', self.mention.value)
        if not m:
            await interaction.response.send_message(
                "❌ Couldn't find a valid mention. Use @Username to mention someone.",
                ephemeral=True,
            )
            return
        member = interaction.guild.get_member(int(m.group(1))) if interaction.guild else None
        if not member:
            await interaction.response.send_message("❌ User not found in this server.", ephemeral=True)
            return

        prompt_str = fmt_prompts(self.prompts)
        view = DMInputView(self.code_str, self.guild_id, self.sudo,
                           self.prompts, self.channel_id)
        try:
            await member.send(
                f"👋 **{interaction.user.display_name}** wants you to provide inputs "
                f"for a Python script.\n\n"
                f"**Inputs needed:**\n{prompt_str}\n\n"
                f"Click below to fill them in:",
                view=view,
            )
            await interaction.response.send_message(
                f"✅ Input request sent to {member.mention}!", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ Couldn't DM {member.mention} — they may have DMs disabled.",
                ephemeral=True,
            )


# ── DMInputView — button inside the DM ───────────────────────────────────────

class DMInputView(discord.ui.View):
    def __init__(self, code: str, guild_id: str, sudo: bool,
                 prompts: list, channel_id: int):
        super().__init__(timeout=600)
        self.code_str   = code
        self.guild_id   = guild_id
        self.sudo       = sudo
        self.prompts    = prompts
        self.channel_id = channel_id

    @discord.ui.button(label="Fill Inputs", style=discord.ButtonStyle.primary, emoji="✏️")
    async def fill(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            InputModal(self.code_str, self.guild_id, self.sudo,
                       prompts=self.prompts, post_to_channel_id=self.channel_id)
        )
        button.disabled = True
        await interaction.message.edit(view=self)


# ── PublicInputView — button in the public channel message ───────────────────

class PublicInputView(discord.ui.View):
    def __init__(self, code: str, guild_id: str, sudo: bool, prompts: list):
        super().__init__(timeout=600)
        self.code_str = code
        self.guild_id = guild_id
        self.sudo     = sudo
        self.prompts  = prompts

    @discord.ui.button(label="Fill Inputs", style=discord.ButtonStyle.success, emoji="✏️")
    async def fill(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            InputModal(self.code_str, self.guild_id, self.sudo, prompts=self.prompts)
        )


# ── CodeModal ─────────────────────────────────────────────────────────────────

class CodeModal(discord.ui.Modal, title="Run Python Code"):
    code = discord.ui.TextInput(
        label="Python code",
        style=discord.TextStyle.paragraph,
        placeholder="print('Hello, world!')",
        max_length=4000,
        required=True,
    )

    def __init__(self, guild_id: str, sudo: bool):
        super().__init__()
        self.guild_id = guild_id
        self.sudo     = sudo

    async def on_submit(self, interaction: discord.Interaction):
        code_str = self.code.value.strip()
        if not code_str:
            await interaction.response.send_message("No code provided.", ephemeral=True)
            return
        if not self.sudo:
            err = check_code_safety(code_str)
            if err:
                await interaction.response.send_message(f"🚫 **Blocked:** {err}", ephemeral=True)
                return

        if needs_input(code_str):
            prompts = extract_input_prompts(code_str)
            prompt_str = fmt_prompts(prompts)
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(
                f"Your code needs **{len(prompts)}** input(s):\n{prompt_str}\n\n"
                f"How would you like to collect them?",
                view=InputMethodView(code_str, self.guild_id, self.sudo,
                                     prompts, interaction.channel_id),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        output = await run_and_format(code_str, self.guild_id, self.sudo, "", interaction.user)
        await interaction.followup.send(output)


# ── Commands ──────────────────────────────────────────────────────────────────

@tree.command(name="run", description="Run Python code in a sandboxed terminal")
@app_commands.describe(file="Upload a .py file to run (supports indentation)")
async def run(interaction: discord.Interaction, file: discord.Attachment = None):
    guild_id = str(interaction.guild_id) if interaction.guild_id else "dm"
    if interaction.guild_id and is_bot_disabled(guild_id):
        await interaction.response.send_message("Python runner is disabled for this server.", ephemeral=True)
        return
    # Check access: if not public, require admin
    if interaction.guild_id and not is_scripts_public(guild_id) and not is_admin(interaction):
        await interaction.response.send_message(
            "Python runner is currently admin-only. An admin can enable public access via `/hub python`.",
            ephemeral=True
        )
        return
    sudo = is_sudo_enabled(guild_id) if interaction.guild_id else False

    # File upload path — supports proper indentation
    if file is not None:
        if not file.filename.endswith(".py"):
            await interaction.response.send_message("Please upload a `.py` file.", ephemeral=True)
            return
        if file.size > 50_000:
            await interaction.response.send_message("File too large (max 50 KB).", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            code_bytes = await file.read()
            code_str = code_bytes.decode("utf-8")
        except Exception as e:
            await interaction.followup.send(f"Could not read file: {e}", ephemeral=True)
            return

        if not sudo:
            err = check_code_safety(code_str)
            if err:
                await interaction.followup.send(f"🚫 **Blocked:** {err}", ephemeral=True)
                return

        # If the code calls input(), show the method picker
        if needs_input(code_str):
            prompts = extract_input_prompts(code_str)
            prompt_str = fmt_prompts(prompts)
            await interaction.followup.send(
                f"Your code needs **{len(prompts)}** input(s):\n{prompt_str}\n\n"
                f"How would you like to collect them?",
                view=InputMethodView(code_str, guild_id, sudo,
                                     prompts, interaction.channel_id),
                ephemeral=True,
            )
            return

        output = await run_and_format(code_str, guild_id, sudo, "", interaction.user)
        await interaction.followup.send(output)
        return

    # No file — show the modal for quick one-liners
    await interaction.response.send_modal(CodeModal(guild_id, sudo))


@tree.command(name="pyhelp", description="Show Python runner info and restrictions")
async def pyhelp(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id) if interaction.guild_id else "dm"
    sudo     = is_sudo_enabled(guild_id) if interaction.guild_id else False
    public   = is_scripts_public(guild_id) if interaction.guild_id else True

    blocked = ", ".join(f"`{b}`" for b in sorted(BLOCKED_IMPORTS))
    embed = discord.Embed(
        title="🐍 Python Runner",
        color=discord.Color.green() if sudo else discord.Color.blurple()
    )
    embed.add_field(name="Mode", value="**Sudo** (unrestricted)" if sudo else "**Restricted**", inline=True)
    embed.add_field(name="Access", value="**Public**" if public else "**Admin only**", inline=True)
    embed.add_field(name="Timeout", value="8 seconds", inline=True)

    if not sudo:
        embed.add_field(
            name="Blocked modules",
            value=blocked,
            inline=False
        )
        embed.add_field(
            name="Blocked patterns",
            value="`open()`, `__import__()`, `compile()`, `globals()`, `locals()`, `vars()`, `getattr()`, `setattr()`, `delattr()`, `eval()`, `exec()`",
            inline=False
        )
    else:
        embed.add_field(name="Note", value="Sudo mode is active — all imports are allowed.", inline=False)

    embed.set_footer(text="Use /run to open the code editor • Admins can configure via /hub python")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Heartbeat + error handlers ────────────────────────────────────────────────

@tasks.loop(seconds=60)
async def heartbeat_task():
    bot_utils.write_heartbeat("python")

@heartbeat_task.before_loop
async def before_heartbeat():
    await client.wait_until_ready()

@client.event
async def on_error(event_method, *args, **kwargs):
    import traceback
    exc = sys.exc_info()[1]
    guild_id = None
    if args and hasattr(args[0], 'guild') and args[0].guild:
        guild_id = str(args[0].guild.id)
    if isinstance(exc, discord.HTTPException) and exc.status == 429:
        bot_utils.log_event("python", "rate_limit",
            f"Rate limited in {event_method}: {exc}", guild_id=guild_id)
    elif exc:
        bot_utils.log_event("python", "error",
            f"Unhandled error in {event_method}: {traceback.format_exc()[:500]}",
            guild_id=guild_id)

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if isinstance(error, app_commands.CommandInvokeError):
        inner = error.original
        if isinstance(inner, discord.HTTPException) and inner.status == 429:
            bot_utils.log_event("python", "rate_limit",
                f"Rate limited on /{interaction.command.name if interaction.command else '?'}: {inner}",
                guild_id=guild_id)
            if not interaction.response.is_done():
                await interaction.response.send_message("Rate limited. Try again shortly.", ephemeral=True)
            return
    bot_utils.log_event("python", "error",
        f"Command error on /{interaction.command.name if interaction.command else '?'}: {error}",
        guild_id=guild_id)
    if not interaction.response.is_done():
        await interaction.response.send_message("An error occurred.", ephemeral=True)


@client.event
async def on_ready():
    await tree.sync()
    if not heartbeat_task.is_running():
        heartbeat_task.start()
    print(f"Python Bot logged in as {client.user}")


client.run(TOKEN)
