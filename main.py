import os
import shutil
import zipfile
import time
import json
import sqlite3
import asyncio
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

app = Client("AnysnapPaaSManager", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)

USER_STATE = {}
RUNNING_CONTAINERS = {}  # {user_id: {"container_id": str}}
USER_LOCKS = {}          # {user_id: asyncio.Lock()}

def get_user_lock(user_id):
    if user_id not in USER_LOCKS:
        USER_LOCKS[user_id] = asyncio.Lock()
    return USER_LOCKS[user_id]

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
        if not container_id: continue
        try:
            container = docker_client.containers.get(container_id)
            if container.status in ["running", "restarting"]:
                RUNNING_CONTAINERS[user_id] = {"container_id": container_id}
                print(f"✅ Restored user {user_id} -> {container_id[:8]}")
            else:
                save_project(user_id, container_id, p_type, root, "stopped")
        except docker.errors.NotFound:
            save_project(user_id, "", p_type, "", "deleted")
        except Exception as e:
            print(f"Error restoring {container_id}: {e}")

async def async_stop_user_container(user_id):
    """Robust non-blocking container stop. Returns True if successful, False if cleanup failed."""
    if user_id not in RUNNING_CONTAINERS: return True
    container_id = RUNNING_CONTAINERS[user_id]["container_id"]
    try:
        container = await asyncio.to_thread(docker_client.containers.get, container_id)
        try: await asyncio.to_thread(container.stop, timeout=5)
        except Exception: pass
        
        try: 
            await asyncio.to_thread(container.remove, force=True)
        except Exception as e: 
            print(f"Container remove error: {e}")
            return False 
            
    except docker.errors.NotFound:
        pass 
    except Exception as e:
        print(f"Container cleanup error: {e}")
        return False
    
    RUNNING_CONTAINERS.pop(user_id, None)
    return True

async def cleanup_old_user_images(user_id, current_tag):
    """Deletes old images for the user to prevent disk leak."""
    try:
        images = await asyncio.to_thread(docker_client.images.list, filters={"reference": f"anysnap_{user_id}_*"})
        for img in images:
            if current_tag in img.tags:
                continue
            try: await asyncio.to_thread(docker_client.images.remove, image=img.id, force=True)
            except: pass
    except Exception as e:
        print(f"Image cleanup error: {e}")

def check_zip_security(zip_path, extract_to):
    if os.path.getsize(zip_path) > MAX_ZIP_SIZE: raise Exception("ZIP file too large (Max 50MB)")
    base = os.path.abspath(extract_to)
    with zipfile.ZipFile(zip_path, "r") as z:
        infos = z.infolist()
        if len(infos) > MAX_FILES: raise Exception(f"Too many files (Max {MAX_FILES})")
        total_size = sum(i.file_size for i in infos)
        if total_size > MAX_EXTRACTED_SIZE: raise Exception("Extracted size exceeds limit (Max 200MB)")
        
        for info in infos:
            attr = info.external_attr >> 16
            if (attr & 0o120000) == 0o120000: raise Exception(f"Security: Symlinks are not allowed: {info.filename}")
            target = os.path.abspath(os.path.join(extract_to, info.filename))
            if os.path.commonpath([base, target]) != base: raise Exception(f"Security: Malicious path detected: {info.filename}")

def detect_project_entry(bot_dir):
    for root, _, files in os.walk(bot_dir):
        if "Dockerfile" in files:
            return {"type": "docker", "cmd": "", "root": root, "is_auto_dockerfile": False}
        if "package.json" in files:
            try:
                with open(os.path.join(root, "package.json"), 'r') as f:
                    if "start" in json.load(f).get("scripts", {}):
                        return {"type": "node", "cmd": 'CMD ["npm", "start"]', "root": root, "is_auto_dockerfile": False}
            except: pass
        if "go.mod" in files or any(f.endswith('.go') for f in files):
            return {"type": "go", "cmd": 'CMD ["go", "run", "."]', "root": root, "is_auto_dockerfile": False}
        if "Cargo.toml" in files:
            return {"type": "rust", "cmd": 'CMD ["cargo", "run", "--release"]', "root": root, "is_auto_dockerfile": False}
        if "composer.json" in files or "index.php" in files:
            return {"type": "php", "cmd": 'CMD ["php", "-S", "0.0.0.0:8000", "-t", "."]', "root": root, "is_auto_dockerfile": False}

        if "pom.xml" in files:
            return {"type": "java-maven", "cmd": 'CMD ["sh", "-c", "java -jar target/*.jar"]', "root": root, "is_auto_dockerfile": False}
        jar_files = [f for f in files if f.endswith(".jar")]
        if jar_files:
            return {"type": "java-jar", "cmd": f'CMD ["java", "-jar", "{jar_files[0]}"]', "root": root, "is_auto_dockerfile": False}
        if any(f.endswith('.java') for f in files):
            return {"type": "java-single", "cmd": 'CMD ["sh", "-c", "javac *.java && java Main"]', "root": root, "is_auto_dockerfile": False}

        if "requirements.txt" in files or any(f.endswith('.py') for f in files):
            python_priority = ["main.py", "bot.py", "app.py", "server.py", "run.py", "sting.py", "index.py"]
            for file in python_priority:
                if file in files: return {"type": "python", "cmd": f'CMD ["python", "{file}"]', "root": root, "is_auto_dockerfile": False}
            py_files = [f for f in files if f.endswith(".py")]
            if len(py_files) == 1: return {"type": "python", "cmd": f'CMD ["python", "{py_files[0]}"]', "root": root, "is_auto_dockerfile": False}
                
    return None

# ================= KEYBOARDS =================
def get_main_keyboard(user_id):
    kb = [[InlineKeyboardButton("📂 Upload ZIP", callback_data="btn_upload_info")]]
    if user_id in RUNNING_CONTAINERS:
        kb.append([InlineKeyboardButton("🔴 STOP & WIPE", callback_data="btn_stop")])
    elif user_id in USER_STATE and "type" in USER_STATE[user_id]:
        kb.append([InlineKeyboardButton("🚀 DEPLOY TO DOCKER", callback_data="btn_deploy")])
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

# ================= MESSAGE HANDLERS =================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    user_id = message.from_user.id
    if user_id not in USER_STATE: USER_STATE[user_id] = {}
    await message.reply_text(
        "<b>🐳 ANYSNAP UNIVERSAL DOCKER PAAS</b>\n\n"
        "Send a `.zip` file! We auto-detect Python, Node, Go, Rust, PHP, Java, or Dockerfiles.\n"
        "If not detected, you can set a custom runtime.",
        reply_markup=get_main_keyboard(user_id)
    )

@app.on_message(filters.document & filters.private)
async def handle_zip_upload(client, message):
    user_id = message.from_user.id
    lock = get_user_lock(user_id)
    
    if lock.locked():
        return await message.reply_text("⚠️ An operation is already in progress. Please wait.")
        
    async with lock:
        if not message.document.file_name.endswith('.zip'):
            return await message.reply_text("❌ Please send a `.zip` file.")
        if message.document.file_size > MAX_ZIP_SIZE:
            return await message.reply_text(f"❌ ZIP too large. Max {MAX_ZIP_SIZE//1024//1024}MB allowed.")

        status_msg = await message.reply_text("📥 Downloading ZIP...")
        
        # Stop old container before wiping directory
        await async_stop_user_container(user_id)
        
        user_dir = os.path.join(HOST_DIR, str(user_id))
        shutil.rmtree(user_dir, ignore_errors=True)
        os.makedirs(user_dir, exist_ok=True)
        
        zip_path = os.path.join(user_dir, "project.zip")
        extract_dir = os.path.join(user_dir, "extracted")
        
        try:
            await message.download(file_name=zip_path)
            await status_msg.edit_text("🛡️ Checking security and extracting...")
            check_zip_security(zip_path, extract_dir)
            
            with zipfile.ZipFile(zip_path, 'r') as z:
                z.extractall(extract_dir)
            os.remove(zip_path) 
            
            project_data = detect_project_entry(extract_dir)
            
            if project_data:
                USER_STATE[user_id] = {**project_data, "action": None, "dir": user_dir}
                await status_msg.edit_text(
                    f"✅ **Auto-Detected:** `{project_data['type'].upper()}`\n"
                    f"🚀 **Command:** `{project_data['cmd']}`\n\n"
                    "Click Deploy to start your container!",
                    reply_markup=get_main_keyboard(user_id)
                )
            else:
                USER_STATE[user_id] = {"root": extract_dir, "dir": user_dir, "is_auto_dockerfile": True}
                await status_msg.edit_text(
                    "⚠️ **Could not auto-detect project type.**\n"
                    "Please select a runtime environment:",
                    reply_markup=get_runtime_keyboard()
                )
        except Exception as e:
            shutil.rmtree(user_dir, ignore_errors=True)
            await status_msg.edit_text(f"❌ **Error:** {e}")

@app.on_message(filters.text & filters.private)
async def handle_text_input(client, message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id)
    if not state or not state.get("action"): return

    lock = get_user_lock(user_id)
    async with lock:
        if state["action"] == "wait_custom_base":
            state["custom_base"] = message.text.strip()
            state["action"] = "wait_command"
            await message.reply_text(
                f"✅ Base image set to `{state['custom_base']}`.\n"
                "Now send the startup command (e.g. `python main.py`):",
                reply_markup=get_cancel_keyboard()
            )
        elif state["action"] == "wait_command":
            state["cmd"] = message.text.strip()
            state["is_manual"] = True
            state["action"] = None
            await message.reply_text(
                f"✅ Setup complete!\n🚀 **Command:** `{state['cmd']}`",
                reply_markup=get_main_keyboard(user_id)
            )

# ================= CALLBACK HANDLERS =================
@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id
    lock = get_user_lock(user_id)

    if user_id not in USER_STATE: USER_STATE[user_id] = {}

    if data == "btn_cancel":
        async with lock:
            state = USER_STATE.pop(user_id, None)
            if state and state.get("dir"): shutil.rmtree(state["dir"], ignore_errors=True)
            try: await query.message.edit_text("🚫 Action cancelled.", reply_markup=get_main_keyboard(user_id))
            except MessageNotModified: await query.answer("Cancelled")

    elif data == "btn_upload_info":
        await query.answer("📎 Please upload your project ZIP file!", show_alert=True)

    elif data == "btn_stop":
        if lock.locked(): return await query.answer("⚠️ Action in progress...", show_alert=True)
        async with lock:
            success = await async_stop_user_container(user_id)
            if success:
                state = USER_STATE.get(user_id)
                if state and state.get("dir"): shutil.rmtree(state["dir"], ignore_errors=True)
                
                # Full wipe DB state update
                save_project(user_id, "", "", "", "wiped")
                USER_STATE.pop(user_id, None)
                
                await query.message.edit_text("🛑 **Stopped, workspace wiped & DB cleared!**", reply_markup=get_main_keyboard(user_id))
            else:
                await query.answer("⚠️ Failed to remove container fully. Try again.", show_alert=True)

    elif data.startswith("rt_"):
        runtime = data.split("_")[1]
        state = USER_STATE.get(user_id)
        if not state: return await query.answer("Session expired! Upload ZIP again.", show_alert=True)

        async with lock:
            if runtime == "custom":
                state["action"] = "wait_custom_base"
                state["type"] = "custom"
                await query.message.edit_text("⚙️ **Custom Runtime**\nSend base image (e.g., `ubuntu:24.04`):", reply_markup=get_cancel_keyboard())
            elif runtime == "java":
                state["type"] = "java-single"
                state["action"] = "wait_command"
                await query.message.edit_text("☕ **JAVA Selected!**\nSend startup command (e.g., `java -jar app.jar`):", reply_markup=get_cancel_keyboard())
            else:
                state["type"] = runtime
                state["action"] = "wait_command"
                await query.message.edit_text(f"✅ **{runtime.upper()} Selected!**\nSend startup command:", reply_markup=get_cancel_keyboard())

    elif data == "btn_deploy":
        if lock.locked():
            return await query.answer("⚠️ A deployment is already in progress!", show_alert=True)
            
        async with lock:
            state = USER_STATE.get(user_id)
            if not state or "type" not in state: return await query.answer("Session expired! Upload again.", show_alert=True)
            
            project_type, root = state["type"], state["root"]
            image_tag = f"anysnap_{user_id}_{int(time.time())}"
            container = None
            
            success = await async_stop_user_container(user_id)
            if not success:
                return await query.answer("❌ Could not stop existing container. Deployment aborted.", show_alert=True)
            
            try:
                await query.message.edit_text("🏗️ Building Image...\n⏳ This may take a moment.")
                
                dockerfile_path = os.path.join(root, "Dockerfile")
                if state.get("is_auto_dockerfile", False):
                    try: os.remove(dockerfile_path)
                    except: pass

                if not os.path.exists(dockerfile_path):
                    state["is_auto_dockerfile"] = True
                    base_images = {
                        "node": "node:22-alpine", "go": "golang:1.24-alpine", "rust": "rust:1.76-slim",
                        "php": "php:8.2-cli", "python": "python:3.12-slim",
                        "java-maven": "maven:3.9-eclipse-temurin-21",
                        "java-jar": "eclipse-temurin:21-jre-alpine",
                        "java-single": "eclipse-temurin:21-jdk-alpine"
                    }
                    base_img = base_images.get(project_type, state.get("custom_base", "debian:bookworm-slim"))
                    
                    is_manual = state.get("is_manual", False)
                    safe_cmd = f"CMD {json.dumps(['sh', '-c', state['cmd']])}" if is_manual else state["cmd"]
                    
                    install_step = ""
                    if project_type == "python": 
                        req_exists = os.path.exists(os.path.join(root, "requirements.txt"))
                        install_step = (
                            "RUN apt-get update && "
                            "apt-get install -y --no-install-recommends gcc build-essential && "
                            "rm -rf /var/lib/apt/lists/*\n"
                        )
                        # STRICT dependencies check. No silent skips!
                        if req_exists:
                            install_step += "RUN pip install --no-cache-dir -r requirements.txt\n"
                            
                    elif project_type == "node": install_step = "RUN npm install\n"
                    elif project_type == "go": install_step = "RUN go mod download\n"
                    elif project_type == "java-maven": install_step = "RUN mvn clean package -DskipTests\n"
                    
                    df_content = f"FROM {base_img}\nWORKDIR /app\nCOPY . /app/\n{install_step}{safe_cmd}\n"
                    with open(dockerfile_path, "w") as df: df.write(df_content)
                
                await asyncio.to_thread(
                    docker_client.images.build,
                    path=root, tag=image_tag, rm=True, forcerm=True
                )
                
                container = await asyncio.to_thread(
                    docker_client.containers.run,
                    image_tag, detach=True, mem_limit="512m", nano_cpus=500000000
                )
                
                is_stable = True
                for _ in range(5):
                    await asyncio.sleep(2)
                    await asyncio.to_thread(container.reload)
                    if container.status != "running":
                        is_stable = False
                        break
                
                if not is_stable:
                    logs = (await asyncio.to_thread(container.logs, tail=20)).decode("utf-8", errors="ignore")
                    
                    try: await asyncio.to_thread(container.remove, force=True)
                    except: pass
                    try: await asyncio.to_thread(docker_client.images.remove, image=image_tag, force=True)
                    except: pass
                    
                    await query.message.edit_text(
                        f"❌ **Process Crashed!**\n\n"
                        f"Status: `{container.status}`\n\n"
                        f"**Logs:**\n```\n{logs[-3000:]}\n```\n\n"
                        "🔧 Click 'Deploy to Docker' to retry without re-uploading.",
                        reply_markup=get_main_keyboard(user_id)
                    )
                    return

                await asyncio.to_thread(container.update, restart_policy={"Name": "on-failure", "MaximumRetryCount": 3})
                
                RUNNING_CONTAINERS[user_id] = {"container_id": container.id}
                save_project(user_id, container.id, project_type, root, "running")
                
                await cleanup_old_user_images(user_id, current_tag=image_tag)
                
                await query.message.edit_text(
                    "✅ **Deployed & Process Stable!** 🟢\n\n"
                    f"📦 **ID:** `{container.id[:12]}`\n"
                    "*(Note: Stable means the process is alive. Check logs to ensure your app logic is fully working.)*",
                    reply_markup=get_logs_keyboard()
                )
                
            except docker.errors.BuildError as e:
                build_logs = []
                for chunk in e.build_log:
                    if "stream" in chunk: build_logs.append(chunk["stream"])
                    elif "errorDetail" in chunk:
                        detail = chunk["errorDetail"]
                        build_logs.append(detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail))
                    elif "error" in chunk: build_logs.append(str(chunk["error"]))

                logs_text = "".join(build_logs).strip() or str(e)
                try: await asyncio.to_thread(docker_client.images.remove, image=image_tag, force=True)
                except: pass

                await query.message.edit_text(
                    "❌ **Docker Build Failed!**\n\n"
                    f"```\n{logs_text[-3500:]}\n```\n\n"
                    "🔧 Click 'Deploy to Docker' to retry the build.",
                    reply_markup=get_main_keyboard(user_id)
                )
                
            except Exception as e:
                if container is not None:
                    try: await asyncio.to_thread(container.remove, force=True)
                    except: pass
                try: await asyncio.to_thread(docker_client.images.remove, image=image_tag, force=True)
                except: pass
                
                await query.message.edit_text(f"❌ **Deployment Error!**\n\n```\n{str(e)[-3000:]}\n```", reply_markup=get_main_keyboard(user_id))

    elif data.startswith("btn_logs_"):
        if user_id not in RUNNING_CONTAINERS:
            return await query.answer("No running container found!", show_alert=True)
        
        try:
            container_id = RUNNING_CONTAINERS[user_id]["container_id"]
            container = await asyncio.to_thread(docker_client.containers.get, container_id)
            
            if data == "btn_logs_refresh":
                logs_bytes = await asyncio.to_thread(container.logs, tail=30)
                logs = logs_bytes.decode("utf-8", errors="ignore").strip() or "No logs generated yet."
                text = f"📜 **Latest Logs:**\n```\n{logs[-3500:]}\n```\n\nStatus: `{container.status}`"
                try: await query.message.edit_text(text, reply_markup=get_logs_keyboard())
                except MessageNotModified: await query.answer("Logs haven't changed.", show_alert=False)
                    
            elif data == "btn_logs_full":
                await query.answer("Extracting full logs...")
                logs_bytes = await asyncio.to_thread(container.logs)
                logs = logs_bytes.decode("utf-8", errors="ignore")
                
                log_file = f"logs_{container_id[:8]}.txt"
                with open(log_file, "w", encoding="utf-8") as f: f.write(logs)
                await client.send_document(chat_id=user_id, document=log_file, caption=f"📜 Full logs for `{container_id[:8]}`")
                os.remove(log_file)
        except Exception as e:
            await query.answer(f"Error fetching logs: {str(e)[:50]}", show_alert=True)

    elif data == "btn_restart":
        if lock.locked(): return await query.answer("⚠️ Action in progress...", show_alert=True)
        async with lock:
            if user_id not in RUNNING_CONTAINERS:
                return await query.answer("No running container found!", show_alert=True)
            try:
                await query.answer("🔄 Restarting container...")
                container_id = RUNNING_CONTAINERS[user_id]["container_id"]
                container = await asyncio.to_thread(docker_client.containers.get, container_id)
                await asyncio.to_thread(container.restart)
                await query.message.edit_text(f"✅ **Container Restarted!**", reply_markup=get_logs_keyboard())
            except Exception as e:
                await query.answer(f"Error restarting: {str(e)[:50]}", show_alert=True)

if __name__ == "__main__":
    print("🚀 Starting Anysnap PaaS Bot...")
    init_db()
    restore_state()
    app.run()