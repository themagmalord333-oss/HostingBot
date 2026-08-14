import os, shutil, zipfile, ast, sys, subprocess
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
import config

app = Client("LocalHostManager", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)

# System Status tracking
RUNNING_PROCESSES = {}  # Yahan background mein chalne wale bot ka data save hoga
USER_STATE = {}
HOST_DIR = "hosted_bots"

os.makedirs(HOST_DIR, exist_ok=True)

# ================= ZIP EXTRACTOR =================
def safe_extract_zip(zip_path, extract_to):
    abs_extract_to = os.path.abspath(extract_to)
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        for member in zip_ref.namelist():
            member_path = os.path.abspath(os.path.join(extract_to, member))
            if not member_path.startswith(abs_extract_to):
                raise ValueError(f"Path Traversal Detected: {member}")
        zip_ref.extractall(extract_to)

# ================= AST SCANNER (For Auto-installing PIP packages) =================
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
    return [pypi_mapping.get(i, i) for i in imports if i not in std_libs and i]

def detect_entry_file(bot_dir):
    std_files = ["bot.py", "main.py", "app.py", "index.js", "server.js"]
    for root, _, files in os.walk(bot_dir):
        for file in files:
            if file.lower() in std_files:
                return os.path.relpath(os.path.join(root, file), bot_dir)
    return None

# ================= KEYBOARDS =================
def get_main_keyboard(is_running=False):
    buttons = [[InlineKeyboardButton("📂 Upload New Bot (ZIP/PY)", callback_data="dummy")]]
    if is_running:
        buttons.append([InlineKeyboardButton("🔴 STOP RUNNING BOT", callback_data="btn_stop")])
    else:
        buttons.append([InlineKeyboardButton("🚀 DEPLOY & RUN LOCAL", callback_data="btn_deploy")])
    return InlineKeyboardMarkup(buttons)

# ================= COMMANDS =================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    user_id = message.from_user.id
    is_running = user_id in RUNNING_PROCESSES
    await message.reply_text(
        "<b>👑 LOCAL HOSTING MANAGER</b>\n\n"
        "Send any `.py`, `.js`, or `.zip` file.\n"
        "Ye bot files ko Github API par nahi bhejega, balki **is bot ke server/action mein hi usko run karega!** 🚀", 
        reply_markup=get_main_keyboard(is_running)
    )

# ================= UPLOAD MANAGER (ZIP & PY) =================
@app.on_message(filters.document)
async def handle_document(client, message):
    user_id = message.from_user.id
    
    # Purana bot agar chal raha hai toh stop kar do
    if user_id in RUNNING_PROCESSES:
        RUNNING_PROCESSES[user_id].terminate()
        del RUNNING_PROCESSES[user_id]
        
    doc = message.document
    file_ext = doc.file_name.split(".")[-1].lower()

    if file_ext not in ["py", "js", "zip"]:
        return await message.reply_text("❌ Only `.py`, `.js`, or `.zip` files allowed!")

    status = await message.reply_text("📥 Downloading files...")
    
    bot_dir = os.path.join(HOST_DIR, str(user_id))
    if os.path.exists(bot_dir):
        shutil.rmtree(bot_dir)
    os.makedirs(bot_dir, exist_ok=True)
    
    file_path = os.path.join(bot_dir, doc.file_name)
    await message.download(file_path)

    # 1. Zip extraction
    if file_ext == "zip":
        await status.edit_text("📦 Extracting ZIP...")
        try:
            safe_extract_zip(file_path, bot_dir)
            os.remove(file_path)
        except Exception as e:
            return await status.edit_text(f"❌ Zip Extract Error: {e}")

    # 2. Package installation right inside the Github Action
    req_path = os.path.join(bot_dir, "requirements.txt")
    if not os.path.exists(req_path):
        await status.edit_text("🔍 Scanning code for missing pip packages...")
        pkgs = parse_missing_imports(bot_dir)
        if pkgs:
            with open(req_path, "w") as f:
                f.write("\n".join(pkgs))
    
    if os.path.exists(req_path):
        await status.edit_text("⚙️ Installing packages in background...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_path])

    # 3. Detect Entry File
    entry_file = detect_entry_file(bot_dir)
    if not entry_file:
        if file_ext in ["py", "js"]:
            entry_file = doc.file_name
        else:
            USER_STATE[user_id] = {"action": "wait_entry", "dir": bot_dir}
            return await status.edit_text("🚨 **Main file not found!**\nSend the main file name (e.g., `main.py`):")

    USER_STATE[user_id] = {"dir": bot_dir, "entry": entry_file}
    await status.edit_text(
        f"✅ **Files Ready!**\n📂 Main File: `{entry_file}`\n\nClick **DEPLOY** to start it in the background.",
        reply_markup=get_main_keyboard(is_running=False)
    )

# ================= BUTTON CALLBACKS =================
@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id

    if data == "btn_deploy":
        state = USER_STATE.get(user_id)
        if not state or "entry" not in state:
            return await query.answer("No files found. Please upload again.", show_alert=True)
        
        bot_dir = state["dir"]
        entry_file = state["entry"]
        
        await query.message.edit_text("🚀 Spawning process in current Action...")
        
        cmd = [sys.executable if entry_file.endswith(".py") else "node", entry_file]
        try:
            # Popen use kiya hai taaki background mein bot chalta rahe
            process = subprocess.Popen(cmd, cwd=bot_dir)
            RUNNING_PROCESSES[user_id] = process
            await query.message.edit_text("✅ **Bot is now RUNNING in background!** 🟢", reply_markup=get_main_keyboard(is_running=True))
        except Exception as e:
            await query.message.edit_text(f"❌ Failed to start: {e}", reply_markup=get_main_keyboard(is_running=False))

    elif data == "btn_stop":
        process = RUNNING_PROCESSES.get(user_id)
        if process:
            process.terminate()  # Process ko kill karne ke liye
            del RUNNING_PROCESSES[user_id]
            await query.message.edit_text("🛑 **Bot process Stopped!**", reply_markup=get_main_keyboard(is_running=False))
        else:
            await query.answer("Bot is not running.", show_alert=True)
            await query.message.edit_text("🛑 Bot is already stopped.", reply_markup=get_main_keyboard(is_running=False))

# ================= MANUAL ENTRY HANDLER =================
@app.on_message(filters.text & ~filters.command("start"))
async def text_handler(client, message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id)
    if state and state.get("action") == "wait_entry":
        entry = message.text.strip()
        if os.path.exists(os.path.join(state["dir"], entry)):
            USER_STATE[user_id] = {"dir": state["dir"], "entry": entry}
            await message.reply_text(f"✅ **Main File Set:** `{entry}`", reply_markup=get_main_keyboard(is_running=False))
        else:
            await message.reply_text("❌ File not found in zip. Check spelling and try again.")

if __name__ == "__main__":
    print("🚀 Local Action Manager is Online!")
    app.run()