import os
import shutil
import zipfile
import ast
import sys
import time
import json
import re
import asyncio
import subprocess
import google.generativeai as genai
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from pyrogram.errors import MessageNotModified

import config

# ================= SETUP GEMINI AI =================
genai.configure(api_key=config.GEMINI_API_KEY)
ai_model = genai.GenerativeModel('gemini-1.5-flash')

# ================= CONFIG & GLOBALS =================
HOST_DIR = "hosted_bots"
os.makedirs(HOST_DIR, exist_ok=True)

app = Client("SimranHostingRunner", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)

USER_STATE = {}
RUNNING_PROCESSES = {}  # {user_id: {"process": Popen, "log_file": file_obj}}

# ================= UPDATED PACKAGE MAPPING =================
# Simran Hosting Panel ke saare dependencies yahan add kiye hain
PIP_ALIAS = {
    "PIL": "Pillow",
    "telegram": "python-telegram-bot",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "bs4": "beautifulsoup4",
    "telebot": "pyTelegramBotAPI",
    "cryptography": "cryptography",
    "flask": "flask",
    "apscheduler": "APScheduler",
    "github": "PyGithub",
    "psutil": "psutil",
    "requests": "requests",
    "yaml": "PyYAML",
    "Crypto": "pycryptodome"
}

# ================= HELPER FUNCTIONS =================
def cleanup_state(user_id):
    state = USER_STATE.get(user_id)
    if state and "dir" in state and os.path.exists(state["dir"]):
        try: shutil.rmtree(state["dir"])
        except: pass
    if user_id in USER_STATE:
        del USER_STATE[user_id]

def stop_running_bot(user_id):
    if user_id in RUNNING_PROCESSES:
        p_data = RUNNING_PROCESSES[user_id]
        process = p_data.get("process")
        log_file = p_data.get("log_file")

        if process:
            try: process.terminate()
            except: pass
        if log_file and not log_file.closed:
            try: log_file.close()
            except: pass

        del RUNNING_PROCESSES[user_id]
        return True
    return False

def safe_extract_zip(zip_path, extract_to):
    abs_extract_to = os.path.abspath(extract_to)
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        for member in zip_ref.namelist():
            member_path = os.path.abspath(os.path.join(extract_to, member))
            if not member_path.startswith(abs_extract_to):
                raise ValueError(f"Security Alert: Path Traversal Detected in {member}")
        zip_ref.extractall(extract_to)

def get_directory_structure(rootdir):
    dir_tree = ""
    for root, dirs, files in os.walk(rootdir):
        level = root.replace(rootdir, '').count(os.sep)
        indent = ' ' * 4 * (level)
        dir_tree += f"{indent}{os.path.basename(root)}/\n"
        subindent = ' ' * 4 * (level + 1)
        for f in files:
            dir_tree += f"{subindent}{f}\n"
    return dir_tree

def ask_gemini_for_entry(bot_dir):
    tree = get_directory_structure(bot_dir)
    prompt = f"""
    I have extracted a telegram bot's ZIP file. Here is the exact directory tree:
    {tree}

    Analyze this structure intelligently. Find the main entry point file used to start the bot.
    The file could be named anything (e.g., main.py, bot.py, app.py, premiumhosting.py, sting.py, etc.).
    Look for the most logical starting file. Provide its RELATIVE PATH from the root directory.

    Respond ONLY with valid JSON and nothing else. Use this exact format:
    {{
        "entry_file": "relative/path/to/bot.py"
    }}
    """
    try:
        response = ai_model.generate_content(prompt)
        res_text = response.text.strip()
        match = re.search(r'\{.*\}', res_text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return None
    except Exception as e:
        print(f"Gemini AI Error: {e}")
        return None

def parse_missing_imports(bot_dir):
    std_libs = set(sys.builtin_module_names) | set(getattr(sys, "stdlib_module_names", []))
    imports = set()
    for root, _, files in os.walk(bot_dir):
        for file in files:
            if file.endswith(".py"):
                try:
                    with open(os.path.join(root, file), "r", encoding="utf-8", errors="ignore") as f:
                        tree = ast.parse(f.read())
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                imports.add(alias.name.split(".")[0])
                        elif isinstance(node, ast.ImportFrom) and node.module:
                            imports.add(node.module.split(".")[0])
                except: pass
    required = []
    for i in imports:
        if i not in std_libs and i:
            required.append(PIP_ALIAS.get(i, i))
    return required

# ================= KEYBOARDS =================
def get_main_keyboard(user_id):
    is_running = user_id in RUNNING_PROCESSES
    kb = []
    kb.append([InlineKeyboardButton("📂 Upload Bot (ZIP/PY)", callback_data="btn_upload_info")])
    if is_running:
        kb.append([InlineKeyboardButton("🔴 STOP RUNNING BOT", callback_data="btn_stop")])
    else:
        kb.append([InlineKeyboardButton("🚀 DEPLOY & RUN", callback_data="btn_deploy")])
    kb.append([InlineKeyboardButton("🔑 Set Environment Vars", callback_data="btn_set_env")])
    return InlineKeyboardMarkup(kb)

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Cancel", callback_data="btn_cancel")]])

# ================= COMMANDS & CALLBACKS =================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    user_id = message.from_user.id
    if user_id not in USER_STATE: USER_STATE[user_id] = {}
    await message.reply_text(
        "<b>🧠 Simran Hosting Local Runner</b>\n\n"
        "Send `.zip`, `.py`, or `.js` file to host any bot locally.\n"
        "AI automatically detects entry file & installs dependencies.\n"
        "Set environment variables (like BOT_TOKEN) via button below.",
        reply_markup=get_main_keyboard(user_id)
    )

@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id
    if user_id not in USER_STATE: USER_STATE[user_id] = {}

    if data == "btn_cancel":
        cleanup_state(user_id)
        try:
            await query.message.edit_text("🚫 Action cancelled.", reply_markup=get_main_keyboard(user_id))
        except MessageNotModified:
            await query.answer("Already cancelled.", show_alert=True)

    elif data == "btn_upload_info":
        await query.answer("📎 Send file via Paperclip icon!", show_alert=True)

    elif data == "btn_set_env":
        USER_STATE[user_id]["action"] = "wait_env"
        await query.message.edit_text(
            "🔑 **Send Environment Variables**\n\n"
            "Format: `KEY=VALUE` (one per line)\n"
            "Example:\n"
            "`BOT_TOKEN=123456:ABC...`\n"
            "`OWNER_ID=8253072984`\n"
            "`GITHUB_TOKEN=...`\n\n"
            "Send /cancel to abort.",
            reply_markup=get_cancel_keyboard()
        )

    elif data == "btn_deploy":
        state = USER_STATE.get(user_id)
        if not state or "entry" not in state:
            return await query.answer("No files found! Upload a bot first.", show_alert=True)

        bot_dir = state["dir"]
        entry_file = state["entry"]

        # Exact path resolve karo
        exact_entry_path = os.path.normpath(os.path.join(bot_dir, entry_file))
        if not os.path.exists(exact_entry_path):
            target_name = os.path.basename(entry_file)
            for root, _, files in os.walk(bot_dir):
                if target_name in files:
                    exact_entry_path = os.path.join(root, target_name)
                    break

        if not exact_entry_path or not os.path.exists(exact_entry_path):
            try:
                await query.message.edit_text(f"❌ Entry file `{entry_file}` not found!", reply_markup=get_main_keyboard(user_id))
            except MessageNotModified:
                await query.answer("File missing!", show_alert=True)
            return

        exact_entry_path = os.path.abspath(exact_entry_path)

        # Project root find karo (jaha .env ya requirements.txt ho)
        project_root = os.path.dirname(exact_entry_path)
        for root, _, files in os.walk(bot_dir):
            if ".env" in files or "requirements.txt" in files:
                project_root = root
                break
        project_root = os.path.abspath(project_root)

        # 🔥 .env file se environment variables load karo
        bot_env_vars = os.environ.copy()
        env_file_path = os.path.join(project_root, ".env")
        if os.path.exists(env_file_path):
            try:
                with open(env_file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, val = line.split("=", 1)
                            bot_env_vars[key.strip()] = val.strip().strip("'\"")
            except Exception as e:
                print(f".env load error: {e}")

        # Agar user ne manual env vars set kiye hain toh unhe bhi inject karo
        user_env = state.get("env_vars", {})
        for k, v in user_env.items():
            bot_env_vars[k] = v

        # 🔥 Stop existing process agar chal raha hai
        stop_running_bot(user_id)

        cmd = [sys.executable, "-u", exact_entry_path] if exact_entry_path.endswith(".py") else ["node", exact_entry_path]

        try:
            await query.message.edit_text(f"🚀 Starting: `{os.path.basename(exact_entry_path)}`")
        except MessageNotModified:
            pass

        try:
            log_path = os.path.join(project_root, "host_manager.log")
            log_file = open(log_path, "a", buffering=1)

            process = subprocess.Popen(
                cmd,
                cwd=project_root,
                env=bot_env_vars,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL
            )

            RUNNING_PROCESSES[user_id] = {"process": process, "log_file": log_file}

            await asyncio.sleep(3)

            if process.poll() is not None:
                stop_running_bot(user_id)
                try:
                    with open(log_path, "r") as f:
                        log_data = f.read()[-3500:]
                        if not log_data.strip():
                            log_data = "No output captured."
                except Exception:
                    log_data = "Could not read log file."

                error_msg = f"❌ **Bot Crashed!**\n\n**Log:**\n`{log_data}`"
                try:
                    await query.message.edit_text(error_msg, reply_markup=get_main_keyboard(user_id))
                except MessageNotModified:
                    pass
            else:
                try:
                    await query.message.edit_text("✅ **Bot is RUNNING in background!** 🟢", reply_markup=get_main_keyboard(user_id))
                except MessageNotModified:
                    pass

        except Exception as e:
            try:
                await query.message.edit_text(f"❌ Failed to start: {e}", reply_markup=get_main_keyboard(user_id))
            except MessageNotModified:
                await query.answer(f"Error: {e}", show_alert=True)

    elif data == "btn_stop":
        if stop_running_bot(user_id):
            try:
                await query.message.edit_text("🛑 Bot stopped successfully.", reply_markup=get_main_keyboard(user_id))
            except MessageNotModified:
                await query.answer("Stopped.", show_alert=True)
        else:
            await query.answer("Bot not running.", show_alert=True)

# ================= UPLOAD HANDLER =================
@app.on_message(filters.document)
async def handle_document(client, message):
    user_id = message.from_user.id
    doc = message.document
    file_ext = doc.file_name.split(".")[-1].lower()

    if file_ext not in ["py", "js", "zip"]:
        return await message.reply_text("❌ Only `.py`, `.js`, or `.zip` allowed!")

    status = await message.reply_text("📥 Downloading file...")
    cleanup_state(user_id)

    bot_dir = os.path.join(HOST_DIR, f"{user_id}_{int(time.time())}")
    os.makedirs(bot_dir, exist_ok=True)
    file_path = os.path.join(bot_dir, doc.file_name)
    await message.download(file_path)

    if file_ext == "zip":
        await status.edit_text("📦 Extracting ZIP securely...")
        try:
            safe_extract_zip(file_path, bot_dir)
            os.remove(file_path)
        except Exception as e:
            shutil.rmtree(bot_dir)
            return await status.edit_text(f"❌ Extraction Error: {e}")

    # 🔥 Requirements generation with updated mapping
    req_path = None
    for root, _, files in os.walk(bot_dir):
        if "requirements.txt" in files:
            req_path = os.path.join(root, "requirements.txt")
            break

    if not req_path:
        req_path = os.path.join(bot_dir, "requirements.txt")
        await status.edit_text("🔍 Scanning imports for missing packages...")
        pkgs = parse_missing_imports(bot_dir)
        if pkgs:
            with open(req_path, "w") as f:
                f.write("\n".join(pkgs))
            await status.edit_text(f"📦 Found {len(pkgs)} packages to install.")

    # 🔥 Install dependencies
    if req_path and os.path.exists(req_path):
        await status.edit_text("⚙️ Installing dependencies (may take a while)...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req_path],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            shutil.rmtree(bot_dir)
            return await status.edit_text(
                f"❌ **Pip install failed!**\n\n`{result.stderr[-2000:]}`"
            )

    USER_STATE[user_id] = {"dir": bot_dir, "timestamp": time.time()}

    if file_ext == "zip":
        await status.edit_text("🧠 AI is analyzing project structure...")
        ai_result = ask_gemini_for_entry(bot_dir)
        if ai_result and "entry_file" in ai_result:
            entry_file = ai_result["entry_file"]
            USER_STATE[user_id]["entry"] = entry_file
            await status.edit_text(
                f"🧠 **AI found entry:** `{os.path.basename(entry_file)}`\n"
                f"Click **DEPLOY** to run.",
                reply_markup=get_main_keyboard(user_id)
            )
        else:
            USER_STATE[user_id]["action"] = "wait_entry"
            await status.edit_text(
                "🚨 AI couldn't detect entry file.\n"
                "Send the exact filename (e.g., `premiumhosting.py` or `main.py`):",
                reply_markup=get_cancel_keyboard()
            )
    else:
        entry_file = doc.file_name
        USER_STATE[user_id]["entry"] = entry_file
        await status.edit_text(
            f"✅ File ready: `{entry_file}`\nClick **DEPLOY** to run.",
            reply_markup=get_main_keyboard(user_id)
        )

# ================= TEXT HANDLERS =================
@app.on_message(filters.text & ~filters.command(["start", "cancel"]))
async def text_handler(client, message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id)
    text = message.text.strip()

    if not state or "action" not in state:
        return

    action = state["action"]

    if action == "wait_entry":
        USER_STATE[user_id]["entry"] = text
        USER_STATE[user_id].pop("action", None)
        await message.reply_text(f"✅ Entry set to `{text}`\nClick DEPLOY now.", reply_markup=get_main_keyboard(user_id))

    elif action == "wait_env":
        env_dict = {}
        for line in text.split("\n"):
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env_dict[k.strip()] = v.strip().strip("'\"")
        if env_dict:
            USER_STATE[user_id]["env_vars"] = env_dict
            USER_STATE[user_id].pop("action", None)
            await message.reply_text(f"✅ {len(env_dict)} env variables saved!\nClick DEPLOY to apply.", reply_markup=get_main_keyboard(user_id))
        else:
            await message.reply_text("❌ No valid KEY=VALUE pairs found. Try again or /cancel.")

@app.on_message(filters.command("cancel"))
async def cancel_cmd(client, message):
    user_id = message.from_user.id
    cleanup_state(user_id)
    await message.reply_text("🚫 Cancelled.", reply_markup=get_main_keyboard(user_id))

if __name__ == "__main__":
    print("🚀 Simran Hosting Local Runner is Online!")
    app.run()