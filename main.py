import os
import shutil
import zipfile
import ast
import sys
import time
import json
import subprocess
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

import config

# ================= CONFIG & GLOBALS =================
HOST_DIR = "hosted_bots"
os.makedirs(HOST_DIR, exist_ok=True)

app = Client("LocalHostManager", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)

USER_STATE = {}
RUNNING_PROCESSES = {}  # Background mein chalne wale bots ka data

# ================= HELPER FUNCTIONS =================
def cleanup_state(user_id):
    state = USER_STATE.get(user_id)
    if state and "dir" in state and os.path.exists(state["dir"]):
        try: shutil.rmtree(state["dir"])
        except: pass
    if user_id in USER_STATE:
        del USER_STATE[user_id]

def safe_extract_zip(zip_path, extract_to):
    abs_extract_to = os.path.abspath(extract_to)
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        for member in zip_ref.namelist():
            member_path = os.path.abspath(os.path.join(extract_to, member))
            if not member_path.startswith(abs_extract_to):
                raise ValueError(f"Security Alert: Path Traversal Detected in {member}")
        zip_ref.extractall(extract_to)
    
    # 🔥 YAHAN FIX KIYA HAI: Agar ZIP ke andar ek main folder hai, toh usko bahar nikal lo
    extracted_items = os.listdir(extract_to)
    if len(extracted_items) == 1:
        single_folder = os.path.join(extract_to, extracted_items[0])
        if os.path.isdir(single_folder):
            for item in os.listdir(single_folder):
                shutil.move(os.path.join(single_folder, item), extract_to)
            os.rmdir(single_folder)

def get_local_modules(bot_dir):
    local_modules = set()
    for root, dirs, files in os.walk(bot_dir):
        for d in dirs: local_modules.add(d)
        for f in files:
            if f.endswith(".py"): local_modules.add(f[:-3])
    return local_modules

def parse_missing_imports(bot_dir):
    std_libs = set(sys.builtin_module_names) | set(getattr(sys, "stdlib_module_names", []))
    pypi_mapping = {"PIL": "Pillow", "telegram": "python-telegram-bot", "cv2": "opencv-python", "dotenv": "python-dotenv", "bs4": "beautifulsoup4"}
    local_modules = get_local_modules(bot_dir)
    imports = set()
    for root, _, files in os.walk(bot_dir):
        for file in files:
            if file.endswith(".py"):
                try:
                    with open(os.path.join(root, file), "r", encoding="utf-8", errors="ignore") as f:
                        tree = ast.parse(f.read())
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names: imports.add(alias.name.split(".")[0])
                        elif isinstance(node, ast.ImportFrom) and node.module:
                            imports.add(node.module.split(".")[0])
                except Exception: pass
    required = [pypi_mapping.get(i, i) for i in imports if i not in std_libs and i not in local_modules and i]
    return required

def detect_entry_file(bot_dir):
    pkg_json_path = os.path.join(bot_dir, "package.json")
    if os.path.exists(pkg_json_path):
        try:
            with open(pkg_json_path, "r") as f:
                data = json.load(f)
                if "main" in data and os.path.exists(os.path.join(bot_dir, data["main"])):
                    return data["main"]
        except: pass
    std_files = ["bot.py", "main.py", "app.py", "index.js", "server.js"]
    for root, _, files in os.walk(bot_dir):
        for file in files:
            if file.lower() in std_files:
                return os.path.relpath(os.path.join(root, file), bot_dir)
    return None

# ================= KEYBOARDS =================
def get_main_keyboard(user_id):
    is_running = user_id in RUNNING_PROCESSES
    kb = [
        [InlineKeyboardButton("📂 Upload New Bot (ZIP/PY)", callback_data="btn_upload_info")]
    ]
    if is_running:
        kb.append([InlineKeyboardButton("🔴 STOP RUNNING BOT", callback_data="btn_stop")])
    else:
        kb.append([InlineKeyboardButton("🚀 DEPLOY & RUN LOCAL", callback_data="btn_deploy")])
    return InlineKeyboardMarkup(kb)

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Cancel", callback_data="btn_cancel")]])

# ================= COMMANDS & CALLBACKS =================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    user_id = message.from_user.id
    if user_id not in USER_STATE: USER_STATE[user_id] = {}
    await message.reply_text(
        "<b>👑 LOCAL HOSTING MANAGER</b>\n\n"
        "Send any `.py`, `.js`, or `.zip` file via 📎 (Paperclip) icon.\n"
        "Ye bot files ko Github par nahi bhejega, balki yahi server par run karega! 🚀", 
        reply_markup=get_main_keyboard(user_id)
    )

@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id
    if user_id not in USER_STATE: USER_STATE[user_id] = {}

    if data == "btn_cancel":
        cleanup_state(user_id)
        await query.message.edit_text("🚫 Action cancelled.", reply_markup=get_main_keyboard(user_id))

    elif data == "btn_upload_info":
        await query.answer("📎 File upload karne ke liye niche 'Paperclip' (Attachment) icon par click karein!", show_alert=True)

    elif data == "btn_deploy":
        state = USER_STATE.get(user_id)
        if not state or "entry" not in state:
            return await query.answer("No files found! Please upload a ZIP or PY file first.", show_alert=True)
        
        bot_dir = state["dir"]
        entry_file = state["entry"]
        
        # 🔥 YAHAN BHI FIX KIYA HAI: Direct sahi folder location set hoga
        full_entry_path = os.path.join(bot_dir, entry_file)
        actual_cwd = os.path.dirname(full_entry_path)
        actual_entry_name = os.path.basename(full_entry_path)

        if user_id in RUNNING_PROCESSES:
            try: RUNNING_PROCESSES[user_id].terminate()
            except: pass

        await query.message.edit_text("🚀 Spawning process in background...")
        
        cmd = [sys.executable if actual_entry_name.endswith(".py") else "node", actual_entry_name]
        try:
            # Sahi working directory (actual_cwd) use ho rahi hai ab
            process = subprocess.Popen(cmd, cwd=actual_cwd)
            RUNNING_PROCESSES[user_id] = process
            await query.message.edit_text("✅ **Bot is now RUNNING in background!** 🟢", reply_markup=get_main_keyboard(user_id))
        except Exception as e:
            await query.message.edit_text(f"❌ Failed to start: {e}", reply_markup=get_main_keyboard(user_id))

    elif data == "btn_stop":
        process = RUNNING_PROCESSES.get(user_id)
        if process:
            try: process.terminate()
            except: pass
            del RUNNING_PROCESSES[user_id]
            await query.message.edit_text("🛑 **Bot process Stopped!**", reply_markup=get_main_keyboard(user_id))
        else:
            await query.answer("Bot is not running.", show_alert=True)
            await query.message.edit_text("🛑 Bot is already stopped.", reply_markup=get_main_keyboard(user_id))

# ================= UPLOAD MANAGER =================
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
        await status.edit_text("📦 Extracting ZIP safely...")
        try:
            safe_extract_zip(file_path, bot_dir)
            os.remove(file_path)
        except Exception as e:
            shutil.rmtree(bot_dir)
            return await status.edit_text(f"❌ Extraction Error: {e}")

    req_path = os.path.join(bot_dir, "requirements.txt")
    if not os.path.exists(req_path):
        await status.edit_text("🔍 Scanning code for missing pip packages...")
        pkgs = parse_missing_imports(bot_dir)
        if pkgs:
            with open(req_path, "w") as f: f.write("\n".join(pkgs))

    if os.path.exists(req_path):
        await status.edit_text("⚙️ Installing packages via pip...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_path])

    entry_file = detect_entry_file(bot_dir)
    USER_STATE[user_id] = {"dir": bot_dir, "timestamp": time.time()}

    if not entry_file:
        if file_ext in ["py", "js"]: 
            entry_file = doc.file_name
        else:
            USER_STATE[user_id]["action"] = "wait_entry"
            return await status.edit_text("🚨 **Main file not found!**\nSend the exact file name (e.g. `main.py`):", reply_markup=get_cancel_keyboard())

    USER_STATE[user_id]["entry"] = entry_file
    await status.edit_text(
        f"✅ **Files Ready!**\n📂 Main Entry: `{entry_file}`\n\nClick **DEPLOY & RUN LOCAL** to start your bot.",
        reply_markup=get_main_keyboard(user_id)
    )

# ================= TEXT STATE HANDLER =================
@app.on_message(filters.text & ~filters.command(["start", "cancel"]))
async def text_handler(client, message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id)
    text = message.text.strip()
    if not state or "action" not in state: return

    action = state["action"]
    if action == "wait_entry":
        if os.path.exists(os.path.join(state["dir"], text)):
            USER_STATE[user_id]["entry"] = text
            USER_STATE[user_id].pop("action", None)
            await message.reply_text(f"✅ **Main File Set:** `{text}`", reply_markup=get_main_keyboard(user_id))
        else:
            await message.reply_text("❌ File not found in directory. Check spelling and send again:")

if __name__ == "__main__":
    print("🚀 Local Host Manager Bot is Starting...")
    app.run()