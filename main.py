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

app = Client("LocalHostManager", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)

USER_STATE = {}
RUNNING_PROCESSES = {}  # Store: {user_id: {"process": Popen_obj, "log_file": file_obj}}

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
    The file could be named anything (e.g., main.py, bot.py, app.py, sting.py, etc.).
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
    pypi_mapping = {"PIL": "Pillow", "telegram": "python-telegram-bot", "cv2": "opencv-python", "dotenv": "python-dotenv", "bs4": "beautifulsoup4"}
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
                except: pass
    required = [pypi_mapping.get(i, i) for i in imports if i not in std_libs and i]
    return required

# ================= KEYBOARDS =================
def get_main_keyboard(user_id):
    is_running = user_id in RUNNING_PROCESSES
    kb = [[InlineKeyboardButton("📂 Upload New Bot (ZIP/PY)", callback_data="btn_upload_info")]]
    if is_running: kb.append([InlineKeyboardButton("🔴 STOP RUNNING BOT", callback_data="btn_stop")])
    else: kb.append([InlineKeyboardButton("🚀 DEPLOY & RUN LOCAL", callback_data="btn_deploy")])
    return InlineKeyboardMarkup(kb)

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Cancel", callback_data="btn_cancel")]])

# ================= COMMANDS & CALLBACKS =================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    user_id = message.from_user.id
    if user_id not in USER_STATE: USER_STATE[user_id] = {}
    await message.reply_text(
        "<b>👑 AI-POWERED HOSTING MANAGER</b>\n\n"
        "Send any `.zip`, `.py`, or `.js` file via 📎 (Paperclip) icon.\n"
        "Ye bot khud file dhoondhkar chalayega! 🧠🚀", 
        reply_markup=get_main_keyboard(user_id)
    )

@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id
    if user_id not in USER_STATE: USER_STATE[user_id] = {}

    if data == "btn_cancel":
        cleanup_state(user_id)
        try: await query.message.edit_text("🚫 Action cancelled.", reply_markup=get_main_keyboard(user_id))
        except MessageNotModified: await query.answer("Already cancelled.", show_alert=True)

    elif data == "btn_upload_info":
        await query.answer("📎 File upload karne ke liye niche 'Paperclip' icon par click karein!", show_alert=True)

    elif data == "btn_deploy":
        state = USER_STATE.get(user_id)
        if not state or "entry" not in state:
            return await query.answer("No files found! Please upload a ZIP/PY file first.", show_alert=True)
        
        bot_dir = state["dir"]
        entry_file = state["entry"]
        
        # 1. Entry file ka exact path nikalna
        exact_entry_path = os.path.normpath(os.path.join(bot_dir, entry_file))
        
        # Agar Gemini ka path thoda galat ho, toh fallback search karega
        if not os.path.exists(exact_entry_path):
            target_name = os.path.basename(entry_file)
            for root, _, files in os.walk(bot_dir):
                if target_name in files:
                    exact_entry_path = os.path.join(root, target_name)
                    break
                
        if not exact_entry_path or not os.path.exists(exact_entry_path):
            try: return await query.message.edit_text(f"❌ Deploy Failed: Entry file `{entry_file}` not found anywhere in the ZIP!", reply_markup=get_main_keyboard(user_id))
            except MessageNotModified: return await query.answer("File missing!", show_alert=True)
            
        # 2. SMART PROJECT ROOT: Jaha .env ya requirements.txt ho, wahi asli root hai
        project_root = os.path.dirname(exact_entry_path)
        for root, _, files in os.walk(bot_dir):
            if ".env" in files or "requirements.txt" in files:
                project_root = root
                break

        # 3. .ENV FILE INJECTOR: .env ko read karke environment variables me daalna
        bot_env_vars = os.environ.copy()
        env_file_path = os.path.join(project_root, ".env")
        if os.path.exists(env_file_path):
            try:
                with open(env_file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        # Ignore comments and empty lines
                        if line and not line.startswith("#") and "=" in line:
                            key, val = line.split("=", 1)
                            bot_env_vars[key.strip()] = val.strip().strip("'\"")
            except Exception as e:
                print(f"Failed to load .env file: {e}")

        stop_running_bot(user_id)

        # Ab hum exact file chalayenge, lekin Working Directory project_root rakhenge
        cmd = [sys.executable, "-u", exact_entry_path] if exact_entry_path.endswith(".py") else ["node", exact_entry_path]
        
        try: 
            await query.message.edit_text(f"🚀 Spawning process...\n📂 Root: `{os.path.basename(project_root)}`\n📄 File: `{os.path.basename(exact_entry_path)}`")
        except MessageNotModified: 
            pass
        
        try:
            log_path = os.path.join(project_root, "host_manager.log")
            log_file = open(log_path, "a", buffering=1)
            
            # 🔥 Fix: env=bot_env_vars aur cwd=project_root apply kiya gaya hai
            process = subprocess.Popen(
                cmd, 
                cwd=project_root,
                env=bot_env_vars,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL
            )
            
            RUNNING_PROCESSES[user_id] = {"process": process, "log_file": log_file}
            
            # Crash Detection check after 3 seconds
            await asyncio.sleep(3)
            
            if process.poll() is not None:
                stop_running_bot(user_id)
                try:
                    with open(log_path, "r") as f:
                        log_data = f.read()[-3500:] 
                        if not log_data.strip(): log_data = "No output captured."
                except Exception:
                    log_data = "Could not read log file."
                    
                error_msg = f"❌ **Bot Crashed Immediately!**\n\n**Log Output:**\n`{log_data}`"
                try: await query.message.edit_text(error_msg, reply_markup=get_main_keyboard(user_id))
                except MessageNotModified: pass
            else:
                try: await query.message.edit_text("✅ **Bot is now RUNNING in background!** 🟢", reply_markup=get_main_keyboard(user_id))
                except MessageNotModified: pass

        except Exception as e:
            try: await query.message.edit_text(f"❌ Failed to start process: {e}", reply_markup=get_main_keyboard(user_id))
            except MessageNotModified: await query.answer(f"Failed: {e}", show_alert=True)

    elif data == "btn_stop":
        if stop_running_bot(user_id):
            try: await query.message.edit_text("🛑 **Bot process Stopped & Killed safely!**", reply_markup=get_main_keyboard(user_id))
            except MessageNotModified: await query.answer("Bot is already stopped.", show_alert=True)
        else:
            await query.answer("Bot is not running.", show_alert=True)

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

    req_path = None
    for root, _, files in os.walk(bot_dir):
        if "requirements.txt" in files:
            req_path = os.path.join(root, "requirements.txt")
            break
            
    if not req_path:
        req_path = os.path.join(bot_dir, "requirements.txt")
        await status.edit_text("🔍 Scanning code for missing pip packages...")
        pkgs = parse_missing_imports(bot_dir)
        if pkgs:
            with open(req_path, "w") as f: f.write("\n".join(pkgs))

    if req_path and os.path.exists(req_path):
        await status.edit_text("⚙️ Installing packages via pip...")
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_path], capture_output=True, text=True)
        
        if result.returncode != 0:
            shutil.rmtree(bot_dir)
            return await status.edit_text(
                "❌ **Package installation failed!**\n\n"
                f"**Error Log:**\n`{result.stderr[-3500:]}`\n\nPlease check your ZIP's requirements."
            )

    USER_STATE[user_id] = {"dir": bot_dir, "timestamp": time.time()}
    
    if file_ext == "zip":
        await status.edit_text("🧠 **Gemini AI** is analyzing your project structure...")
        ai_result = ask_gemini_for_entry(bot_dir)
        
        if ai_result and "entry_file" in ai_result:
            entry_file = ai_result["entry_file"] 
            USER_STATE[user_id]["entry"] = entry_file
            await status.edit_text(
                f"🧠 **AI Analysis Complete!**\n"
                f"📂 Entry File: `{os.path.basename(entry_file)}`\n\n"
                f"Click **DEPLOY & RUN LOCAL** to start it.",
                reply_markup=get_main_keyboard(user_id)
            )
        else:
            USER_STATE[user_id]["action"] = "wait_entry"
            await status.edit_text("🚨 AI could not detect main file!\nSend EXACT file name manually (e.g. `main.py` or `sting.py`):", reply_markup=get_cancel_keyboard())
    else:
        entry_file = doc.file_name
        USER_STATE[user_id]["entry"] = entry_file
        await status.edit_text(
            f"✅ **File Ready!**\n📂 Main Entry: `{entry_file}`\n\nClick **DEPLOY** to start.",
            reply_markup=get_main_keyboard(user_id)
        )

# ================= MANUAL TEXT OVERRIDE =================
@app.on_message(filters.text & ~filters.command(["start", "cancel"]))
async def text_handler(client, message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id)
    text = message.text.strip()
    
    if not state or "action" not in state: return

    if state["action"] == "wait_entry":
        USER_STATE[user_id]["entry"] = text
        USER_STATE[user_id].pop("action", None)
        await message.reply_text(f"✅ **Main File Set:** `{text}`\n\nClick DEPLOY now!", reply_markup=get_main_keyboard(user_id))

if __name__ == "__main__":
    print("🚀 Ultimate Local Host Manager is Starting...")
    app.run()