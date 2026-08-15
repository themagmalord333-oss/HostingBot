import os
import shutil
import zipfile
import time
import json
import sqlite3
import docker
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from pyrogram.errors import MessageNotModified

import config

# ================= CONFIG & GLOBALS =================
HOST_DIR = "hosted_containers"
DB_FILE = "bot_data.db"
MAX_ZIP_SIZE = 50 * 1024 * 1024       # 50MB
MAX_EXTRACTED_SIZE = 200 * 1024 * 1024 # 200MB
MAX_FILES = 500

os.makedirs(HOST_DIR, exist_ok=True)

try:
    docker_client = docker.from_env()
except Exception as e:
    print(f"❌ Docker Error: Make sure Docker daemon is running! Details: {e}")
    exit(1)

app = Client("DockerPaaSManager", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)

USER_STATE = {}
RUNNING_CONTAINERS = {}  # {user_id: {"container_id": str}}

# ================= DATABASE SETUP =================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            user_id INTEGER PRIMARY KEY,
            container_id TEXT,
            project_type TEXT,
            root_path TEXT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_project(user_id, container_id, p_type, root, status="running"):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''INSERT OR REPLACE INTO projects 
                      (user_id, container_id, project_type, root_path, status) 
                      VALUES (?, ?, ?, ?, ?)''', 
                      (user_id, container_id, p_type, root, status))
    conn.commit()
    conn.close()

def get_all_projects():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM projects")
    rows = cursor.fetchall()
    conn.close()
    return rows

# ================= CORE FUNCTIONS =================
def restore_state():
    print("🔄 Reconstructing container states from DB...")
    db_projects = get_all_projects()
    
    for row in db_projects:
        user_id, container_id, p_type, root, status, _ = row
        try:
            container = docker_client.containers.get(container_id)
            if container.status == "running" or container.status == "restarting":
                RUNNING_CONTAINERS[user_id] = {"container_id": container_id}
                print(f"✅ Restored user {user_id} -> {container_id[:8]}")
            else:
                save_project(user_id, container_id, p_type, root, "stopped")
        except:
            save_project(user_id, container_id, p_type, root, "deleted")

def stop_user_container(user_id):
    """Stops, removes container, and prunes old images"""
    if user_id in RUNNING_CONTAINERS:
        c_data = RUNNING_CONTAINERS[user_id]
        try:
            container = docker_client.containers.get(c_data["container_id"])
            container.stop(timeout=5)
            container.remove(force=True)
        except: pass
        del RUNNING_CONTAINERS[user_id]
        save_project(user_id, "", "", "", "stopped")
    
    # Cleanup old images to prevent disk bloat
    try:
        images = docker_client.images.list(filters={"reference": f"userbot_{user_id}_*"})
        for img in images: docker_client.images.remove(image=img.id, force=True)
    except: pass

def check_zip_security(zip_path):
    if os.path.getsize(zip_path) > MAX_ZIP_SIZE:
        raise Exception("ZIP file too large (Max 50MB)")
    
    with zipfile.ZipFile(zip_path, "r") as z:
        if len(z.namelist()) > MAX_FILES:
            raise Exception(f"Too many files (Max {MAX_FILES})")
        
        total_size = sum(file.file_size for file in z.infolist())
        if total_size > MAX_EXTRACTED_SIZE:
            raise Exception("Extracted size exceeds limit (Max 200MB)")

        for member in z.namelist():
            if ".." in member or member.startswith("/"):
                raise Exception("Security: Malicious path traversal detected")

def detect_project_entry(bot_dir):
    for root, _, files in os.walk(bot_dir):
        if "Dockerfile" in files:
            return {"type": "docker", "cmd": "", "root": root}
            
        if "package.json" in files:
            try:
                with open(os.path.join(root, "package.json"), 'r') as f:
                    if "start" in json.load(f).get("scripts", {}):
                        return {"type": "node", "cmd": 'CMD ["npm", "start"]', "root": root}
            except: pass
            
        if "go.mod" in files or any(f.endswith('.go') for f in files):
            return {"type": "go", "cmd": 'CMD ["go", "run", "."]', "root": root}
            
        if "Cargo.toml" in files:
            return {"type": "rust", "cmd": 'CMD ["cargo", "run", "--release"]', "root": root}
            
        if "composer.json" in files or "index.php" in files:
            return {"type": "php", "cmd": 'CMD ["php", "-S", "0.0.0.0:8000", "-t", "."]', "root": root}

        if "requirements.txt" in files or any(f.endswith('.py') for f in files):
            for file in ["main.py", "bot.py", "app.py", "server.py", "run.py"]:
                if file in files:
                    return {"type": "python", "cmd": f'CMD ["python", "{file}"]', "root": root}
    return None

# ================= KEYBOARDS =================
def get_main_keyboard(user_id):
    is_running = user_id in RUNNING_CONTAINERS
    kb = [[InlineKeyboardButton("📂 Upload ZIP", callback_data="btn_upload_info")]]
    if is_running: kb.append([InlineKeyboardButton("🔴 STOP & WIPE", callback_data="btn_stop")])
    return InlineKeyboardMarkup(kb)

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Cancel", callback_data="btn_cancel")]])

def get_runtime_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🐍 Python", callback_data="rt_python"), InlineKeyboardButton("🟢 Node.js", callback_data="rt_node")],
        [InlineKeyboardButton("🔵 Go", callback_data="rt_go"), InlineKeyboardButton("🦀 Rust", callback_data="rt_rust")],
        [InlineKeyboardButton("☕ Java", callback_data="rt_java"), InlineKeyboardButton("🐘 PHP", callback_data="rt_php")],
        [InlineKeyboardButton("🐧 Custom Base Image", callback_data="rt_custom")],
        [InlineKeyboardButton("🚫 Cancel", callback_data="btn_cancel")]
    ])

def get_logs_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Logs", callback_data="btn_logs_refresh"), InlineKeyboardButton("📜 Full Logs", callback_data="btn_logs_full")],
        [InlineKeyboardButton("🔄 Restart", callback_data="btn_restart"), InlineKeyboardButton("🔴 Stop", callback_data="btn_stop")]
    ])

# ================= COMMANDS & CALLBACKS =================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    user_id = message.from_user.id
    if user_id not in USER_STATE: USER_STATE[user_id] = {}
    await message.reply_text(
        "<b>🐳 UNIVERSAL DOCKER PAAS</b>\n\n"
        "Send a `.zip` file! We auto-detect Python, Node, Go, Rust, PHP or Dockerfiles.\n"
        "If not detected, you can set a custom runtime and startup command.",
        reply_markup=get_main_keyboard(user_id)
    )

@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id
    if user_id not in USER_STATE: USER_STATE[user_id] = {}

    if data == "btn_cancel":
        USER_STATE.pop(user_id, None)
        try: await query.message.edit_text("🚫 Action cancelled.", reply_markup=get_main_keyboard(user_id))
        except MessageNotModified: await query.answer("Cancelled")

    elif data == "btn_upload_info":
        await query.answer("📎 Please upload your project ZIP file!", show_alert=True)

    elif data == "btn_stop":
        stop_user_container(user_id)
        await query.message.edit_text("🛑 **Stopped and cleaned up!**", reply_markup=get_main_keyboard(user_id))

    # --- RUNTIME FALLBACK HANDLERS ---
    elif data.startswith("rt_"):
        runtime = data.split("_")[1]
        state = USER_STATE.get(user_id)
        if not state: return await query.answer("Session expired! Upload ZIP again.", show_alert=True)

        if runtime == "custom":
            state["action"] = "wait_custom_base"
            await query.message.edit_text("⚙️ **Custom Runtime**\nSend base image (e.g., `ubuntu:24.04`):", reply_markup=get_cancel_keyboard())
        else:
            state["type"] = runtime
            state["action"] = "wait_command"
            await query.message.edit_text(f"✅ **{runtime.upper()} Selected!**\nSend startup command (e.g., `python bot.py`):", reply_markup=get_cancel_keyboard())

    # --- DEPLOYMENT LOGIC ---
    elif data == "btn_deploy":
        state = USER_STATE.get(user_id)
        if not state or "type" not in state: return await query.answer("Session expired!", show_alert=True)
        
        await query.message.edit_text("🏗️ Building Secure Container...")
        project_type, root = state["type"], state["root"]
        
        try:
            dockerfile_path = os.path.join(root, "Dockerfile")
            if not os.path.exists(dockerfile_path):
                # 1. Image Resolution
                base_images = {
                    "node": "node:22-alpine", "go": "golang:1.24-alpine", "rust": "rust:1.76-slim",
                    "php": "php:8.2-cli", "python": "python:3.12-slim", "java": "eclipse-temurin:21-jre"
                }
                base_img = base_images.get(project_type, state.get("custom_base", "debian:bookworm-slim"))
                
                # 2. Secure Command Interpolation
                is_manual = state.get("is_manual", False)
                if is_manual:
                    safe_cmd = f"CMD {json.dumps(['sh', '-c', state['cmd']])}"
                else:
                    safe_cmd = state["cmd"] # Auto-detected CMD is already safe formatted
                
                # 3. Generating Dockerfile
                install_step = ""
                if project_type == "python": install_step = "RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi\n"
                elif project_type == "node": install_step = "RUN npm install\n"
                elif project_type == "go": install_step = "RUN go mod download\n"
                
                df_content = f"FROM {base_img}\nWORKDIR /app\nCOPY . /app/\n{install_step}{safe_cmd}\n"
                with open(dockerfile_path, "w") as df: df.write(df_content)
            
            # 4. Build & Run
            image_tag = f"userbot_{user_id}_{int(time.time())}"
            docker_client.images.build(path=root, tag=image_tag, rm=True)
            
            container = docker_client.containers.run(
                image_tag, detach=True, mem_limit="512m", nano_cpus=500000000,
                restart_policy={"Name": "on-failure", "MaximumRetryCount": 3}
            )
            
            # 5. Persistence
            RUNNING_CONTAINERS[user_id] = {"container_id": container.id}
            save_project(user_id, container.id, project_type, root, "running")
            
            await query.message.edit_text(
                "✅ **Deployed & Running!** 🟢\n\n"
                f"📦 **ID:** `{container.id[:12]}`\nUse buttons below to manage.",
                reply_markup=get_logs_keyboard()
            )
            
        except Exception as e:
            stop_user_container(user_id)
            await query.message.edit_text(f"❌ **Deploy Failed:** {e}", reply_markup=get_main_keyboard(user_id))

    # --- LOGS & MANAGEMENT ---
    elif data.startswith("btn_logs_"):
        if user_id not in RUNNING_CONTAINERS: return await query.answer("No running container!", show_alert=True)
        container = docker_client.containers.get(RUNNING_CONTAINERS[user_id]["container_id"])
        
        if data == "btn_logs_refresh":
            logs = container.logs(tail=50).decode("utf-8", errors="ignore").strip() or "No logs yet..."
            try: await query.message.edit_text(f"📜 **Last 50 Lines:**\n```\n{logs}\n```", reply_markup=get_logs_keyboard())
            except MessageNotModified: await query.answer("Logs up to date!")
            
        elif data == "btn_logs_full":
            await query.answer("Generating log file...")
            path = f"logs_{user_id}.txt"
            with open(path, "wb") as f: f.write(container.logs())
            await client.send_document(user_id, path, caption="📜 Full Logs")
            os.remove(path)

    elif data == "btn_restart":
        if user_id in RUNNING_CONTAINERS:
            await query.answer("Restarting...")
            container = docker_client.containers.get(RUNNING_CONTAINERS[user_id]["container_id"])
            container.restart()
            await query.message.edit_text("✅ **Restarted!**", reply_markup=get_logs_keyboard())

# ================= UPLOAD HANDLER =================
@app.on_message(filters.document)
async def handle_document(client, message):
    user_id = message.from_user.id
    if not message.document.file_name.endswith(".zip"): return await message.reply("Only ZIP allowed!")

    stop_user_container(user_id) # Cleanup previous container immediately

    status = await message.reply("📥 Downloading...")
    bot_dir = os.path.join(HOST_DIR, f"{user_id}_{int(time.time())}")
    file_path = os.path.join(bot_dir, message.document.file_name)
    os.makedirs(bot_dir, exist_ok=True)
    await message.download(file_path)

    try:
        check_zip_security(file_path)
        with zipfile.ZipFile(file_path, "r") as z: z.extractall(bot_dir)
        os.remove(file_path)
    except Exception as e:
        shutil.rmtree(bot_dir, ignore_errors=True)
        return await status.edit(f"❌ Security Error: {e}")

    detection = detect_project_entry(bot_dir)
    USER_STATE[user_id] = {"dir": bot_dir, "root": bot_dir}

    if detection:
        USER_STATE[user_id].update(detection)
        await status.edit(
            f"✅ **Auto-Detected {detection['type'].upper()} Project!**\n\nClick DEPLOY.", 
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 DEPLOY", callback_data="btn_deploy")]])
        )
    else:
        USER_STATE[user_id]["action"] = "wait_runtime"
        await status.edit("🚨 **Detection failed!** Choose runtime:", reply_markup=get_runtime_keyboard())

# ================= MANUAL INPUT HANDLER =================
@app.on_message(filters.text & ~filters.command(["start"]))
async def text_handler(client, message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id)
    if not state: return

    if state.get("action") == "wait_custom_base":
        state["custom_base"] = message.text.strip()
        state["type"] = "custom"
        state["action"] = "wait_command"
        await message.reply("✅ Base Image Set! Now send startup command:", reply_markup=get_cancel_keyboard())
        
    elif state.get("action") == "wait_command":
        state["cmd"] = message.text.strip()
        state["is_manual"] = True
        state.pop("action", None)
        await message.reply(
            f"✅ **Command Set:** `{state['cmd']}`", 
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 DEPLOY", callback_data="btn_deploy")]])
        )

# ================= MAIN RUN =================
if __name__ == "__main__":
    print("🚀 Initializing Database & Docker Sync...")
    init_db()
    restore_state()
    print("🤖 Bot is Online!")
    app.run()
