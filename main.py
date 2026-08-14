import os
import shutil
import zipfile
import ast
import sys
import time
import json
import subprocess
import google.generativeai as genai
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

import config

# ================= SETUP GEMINI AI =================
genai.configure(api_key=config.GEMINI_API_KEY)
# Flash model tez aur sasta (free) hota hai
ai_model = genai.GenerativeModel('gemini-1.5-flash')

# ================= CONFIG & GLOBALS =================
HOST_DIR = "hosted_bots"
os.makedirs(HOST_DIR, exist_ok=True)

app = Client("LocalHostManager", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)

USER_STATE = {}
RUNNING_PROCESSES = {} 

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

def get_directory_structure(rootdir):
    """Folder ka poora tree banata hai AI ko dikhane ke liye"""
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
    """Gemini AI se scan karwata hai aur JSON output leta hai"""
    tree = get_directory_structure(bot_dir)
    prompt = f"""
    I have extracted a telegram bot's ZIP file. Here is the exact directory tree:
    {tree}
    
    Analyze this structure intelligently. Find the main entry point file used to start the bot.
    Often it's named main.py, bot.py, __main__.py, app.py, or index.js. It might be inside a subfolder.
    
    Respond STRICTLY with valid JSON. No markdown formatting, no explanations. 
    Required keys:
    "entry_file": "The relative path to the main file from the root directory"
    "run_command": ["python3", "path/to/file.py"] (Provide the exact list of command arguments to run it)
    """
    try:
        response = ai_model.generate_content(prompt)
        res_text = response.text.strip()
        # Clean JSON from markdown if AI adds it
        if res_text.startswith("```json"): res_text = res_text[7:-3]
        elif res_text.startswith("```"): res_text = res_text[3:-3]
        
        return json.loads(res_text.strip())
    except Exception as e:
        print(f"Gemini AI Error: {e}")
        return None

# Pip Packages Scanner
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
        "Gemini AI structure scan karke khud batayega code kaise chalana hai! 🧠🚀", 
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
        await query.answer("📎 File upload karne ke liye niche 'Paperclip' icon par click karein!", show_alert=True)

    elif data == "btn_deploy":
        state = USER_STATE.get(user_id)
        if not state or "run_cmd" not in state:
            return await query.answer("No files found! Please upload a ZIP/PY file first.", show_alert=True)
        
        bot_dir = state["dir"]
        run_cmd = state["run_cmd"] # AI ne jo command di hai wo chalegi
        
        if user_id in RUNNING_PROCESSES:
            try: RUNNING_PROCESSES[user_id].terminate()
            except: pass

        await query.message.edit_text(f"🚀 Spawning process using AI command: `{' '.join(run_cmd)}`")
        
        try:
            # Popen se background mein AI ki di hui command run karenge
            process = subprocess.Popen(run_cmd, cwd=bot_dir)
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

    # 1. Extraction
    if file_ext == "zip":
        await status.edit_text("📦 Extracting ZIP safely...")
        try:
            safe_extract_zip(file_path, bot_dir)
            os.remove(file_path)
        except Exception as e:
            shutil.rmtree(bot_dir)
            return await status.edit_text(f"❌ Extraction Error: {e}")

    # 2. Package Scan
    req_path = os.path.join(bot_dir, "requirements.txt")
    if not os.path.exists(req_path):
        await status.edit_text("🔍 Scanning code for missing pip packages...")
        pkgs = parse_missing_imports(bot_dir)
        if pkgs:
            with open(req_path, "w") as f: f.write("\n".join(pkgs))

    if os.path.exists(req_path):
        await status.edit_text("⚙️ Installing packages via pip...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_path])

    # 3. GEMINI AI SCAN (The Magic)
    USER_STATE[user_id] = {"dir": bot_dir, "timestamp": time.time()}
    
    if file_ext == "zip":
        await status.edit_text("🧠 **Gemini AI** is analyzing your folder structure...")
        ai_result = ask_gemini_for_entry(bot_dir)
        
        if ai_result and "run_command" in ai_result:
            entry_file = ai_result.get("entry_file", "Unknown")
            run_cmd = ai_result["run_command"]
            
            # Subprocess me run karne ke liye list ko theek karte hain
            if "python3" in run_cmd: run_cmd[run_cmd.index("python3")] = sys.executable
            elif "python" in run_cmd: run_cmd[run_cmd.index("python")] = sys.executable
            
            USER_STATE[user_id]["entry"] = entry_file
            USER_STATE[user_id]["run_cmd"] = run_cmd
            
            cmd_str = " ".join(run_cmd)
            await status.edit_text(
                f"🧠 **Gemini AI Analysis Complete!**\n"
                f"📂 Entry File: `{entry_file}`\n"
                f"⚙️ Run Command: `{cmd_str}`\n\n"
                f"Click **DEPLOY & RUN LOCAL** to start it.",
                reply_markup=get_main_keyboard(user_id)
            )
        else:
            USER_STATE[user_id]["action"] = "wait_entry"
            await status.edit_text("🚨 AI could not detect main file!\nSend exact path manually (e.g. `src/main.py`):", reply_markup=get_cancel_keyboard())
    else:
        # For single files, AI is not needed
        entry_file = doc.file_name
        run_cmd = [sys.executable if file_ext == "py" else "node", entry_file]
        USER_STATE[user_id]["entry"] = entry_file
        USER_STATE[user_id]["run_cmd"] = run_cmd
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
        USER_STATE[user_id]["run_cmd"] = [sys.executable if text.endswith(".py") else "node", text]
        USER_STATE[user_id].pop("action", None)
        await message.reply_text(f"✅ **Main File Set:** `{text}`", reply_markup=get_main_keyboard(user_id))

if __name__ == "__main__":
    print("🚀 AI-Powered Host Manager is Starting...")
    app.run()