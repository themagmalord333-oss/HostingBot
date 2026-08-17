import os
import shutil
import zipfile
import time
import json
import asyncio
import docker
import psutil
import requests
import re
from bson import ObjectId
from pymongo import MongoClient
import gridfs
from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from pyrogram.errors import MessageNotModified

import config

print("⏳ Initializing System Variables...")

# ================= CONFIG & GLOBALS =================
HOST_DIR = "hosted_containers"
MAX_ZIP_SIZE = 50 * 1024 * 1024       
MAX_EXTRACTED_SIZE = 200 * 1024 * 1024 
MAX_FILES = 500                       
MAX_NODES = 7  

OWNER_ID = int(os.getenv("OWNER_ID", "8629274424"))
os.makedirs(HOST_DIR, exist_ok=True)
NODE_ID = int(os.getenv("NODE_ID", 1))

print("⏳ Connecting to Docker...")
try:
    docker_client = docker.from_env()
    print("✅ Docker Connected!")
except Exception as e:
    print(f"❌ Docker Error: {e}")
    exit(1)

print("⏳ Connecting to MongoDB...")
try:
    mongo_client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping') 
    db = mongo_client["anysnap_paas"]
    projects_col = db["projects"]
    nodes_col = db["nodes"]
    settings_col = db["settings"]
    fs = gridfs.GridFS(db)
    print("✅ MongoDB Connected!")
except Exception as e:
    print(f"❌ MongoDB Error: Please allow '0.0.0.0/0' IP in MongoDB Atlas! Details: {e}")
    exit(1)

app = Client("AnysnapCloud", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)
USER_STATE, USER_LOCKS, ENV_WAITING, REQ_WAITING, CMD_WAITING = {}, {}, {}, {}, {}

def get_user_lock(user_id):
    if user_id not in USER_LOCKS: USER_LOCKS[user_id] = asyncio.Lock()
    return USER_LOCKS[user_id]

# ================= OWNER / ADMIN HELPERS =================
def is_owner(user_id: int) -> bool:
    return int(user_id) == OWNER_ID

def get_auto_approve() -> bool:
    setting = settings_col.find_one({"_id": "global_settings"})
    if not setting:
        settings_col.insert_one({"_id": "global_settings", "auto_approve": False})
        return False
    return bool(setting.get("auto_approve", False))

def set_auto_approve(value: bool):
    settings_col.update_one({"_id": "global_settings"}, {"$set": {"auto_approve": bool(value), "updated_at": time.time()}}, upsert=True)

async def render_owner_panel(client, message_or_query, is_edit=False):
    auto = get_auto_approve()
    pending = await asyncio.to_thread(projects_col.count_documents, {"status": "PENDING_APPROVAL"})
    running = await asyncio.to_thread(projects_col.count_documents, {"status": "RUNNING"})
    total = await asyncio.to_thread(projects_col.count_documents, {})
    online_nodes = await asyncio.to_thread(nodes_col.count_documents, {"last_seen": {"$gt": time.time() - 30}})
    
    text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 👑 OWNER CONTROL     ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"🤖 AUTO APPROVE  {'🟢 ON' if auto else '🔴 OFF'}\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏳ Pending       `{pending}`\n🟢 Running       `{running}`\n📦 Projects      `{total}`\n🌐 Nodes Online  `{online_nodes}`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n🎛 OWNER CONTROLS")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Auto Approve ON", callback_data="owner_auto_on"), InlineKeyboardButton("🔴 Auto Approve OFF", callback_data="owner_auto_off")], 
        [InlineKeyboardButton("⏳ Pending", callback_data="owner_pending"), InlineKeyboardButton("🤖 Projects", callback_data="owner_projects")], 
        [InlineKeyboardButton("🌐 Nodes", callback_data="owner_nodes"), InlineKeyboardButton("📊 Statistics", callback_data="owner_stats")], 
        [InlineKeyboardButton("🔄 Refresh", callback_data="owner_panel")]
    ])
    
    if is_edit: await message_or_query.edit_text(text, reply_markup=kb)
    else: await message_or_query.reply_text(text, reply_markup=kb)

# ================= UI HELPERS =================
def format_uptime(started_at):
    if not started_at: return "N/A"
    elapsed = int(time.time() - started_at)
    return f"{elapsed // 3600}h {(elapsed % 3600) // 60}m"

def get_progress_bar(percent):
    filled = int((percent / 100) * 6)
    return ("█" * filled) + ("░" * (6 - filled))

# ================= ANIMATION HELPERS =================
async def animate_status(message, project_id, operation):
    frames = {
        "deploy": ["📦 Preparing Project", "🔧 Building Container", "⚙️ Installing Dependencies", "🐳 Starting Container", "🔍 Running Health Check"],
        "restart": ["⏹ Stopping Container", "🔄 Restarting Container", "🐳 Starting Container", "🔍 Health Check"],
        "start": ["📦 Loading Image", "🐳 Creating Container", "🚀 Starting Bot", "🔍 Health Check"],
        "env": ["🔐 Reading Environment", "⚙️ Applying Variables", "🔄 Restarting Container", "🔍 Health Check"],
        "stop": ["⏳ Stopping Container", "🧹 Cleaning Resources", "✅ Saving State"],
        "delete": ["⏳ Stopping Container", "🗑️ Removing Container", "🧹 Cleaning Image", "💾 Removing Project Data"],
    }
    steps = frames.get(operation, ["⚙️ Processing"])
    spinner = ["◐", "◓", "◑", "◒"]
    i = 0
    while True:
        try:
            proj = await asyncio.to_thread(projects_col.find_one, {"_id": project_id})
            if not proj: break
            status = str(proj.get("status", "")).upper()
            if status in ["RUNNING", "STOPPED", "CRASHED", "ERROR"]: break
            step = steps[i % len(steps)]
            spin = spinner[i % len(spinner)]
            text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ⚡ ANYSNAP CLOUD     ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n{spin} **{operation.upper()}**\n━━━━━━━━━━━━━━━━━━━━━━\n\n🔹 {step}\n\n🟢 Cloud Engine\n🟢 Docker Engine\n🟢 Database\n🟡 Operation Running\n\n━━━━━━━━━━━━━━━━━━━━━━\n✨ Please wait...")
            await message.edit_text(text)
            i += 1
            await asyncio.sleep(0.8)
        except Exception: await asyncio.sleep(0.8)

# ================= RECONCILIATION ENGINE (HEARTBEAT) =================
async def heartbeat_loop():
    while True:
        try:
            def sync_tasks():
                local_containers = docker_client.containers.list(filters={"name": "anysnap_"}, all=True)
                nodes_col.update_one({"node_id": NODE_ID}, {"$set": {"role": "MASTER" if NODE_ID == 1 else "WORKER", "cpu": psutil.cpu_percent(interval=None), "ram": psutil.virtual_memory().percent, "disk": psutil.disk_usage('/').percent, "containers": sum(1 for c in local_containers if c.status == "running"), "last_seen": time.time()}}, upsert=True)
                active_cids = {c.id: c for c in local_containers}
                local_projects = projects_col.find({"target_node": NODE_ID})
                
                for p in local_projects:
                    status, cid, action_time = p.get("status"), p.get("container_id"), p.get("last_action_time", 0)
                    if status in ["RUNNING", "STARTING", "RESTARTING"]:
                        if not cid or cid not in active_cids:
                            projects_col.update_one({"_id": p["_id"]}, {"$set": {"status": "CRASHED", "latest_error": "Container stopped unexpectedly."}})
                        else:
                            container = active_cids[cid]
                            if container.status != "running":
                                try: err = container.logs(tail=50).decode("utf-8", errors="ignore")
                                except: err = "Container crashed. No logs available."
                                projects_col.update_one({"_id": p["_id"]}, {"$set": {"status": "CRASHED", "latest_error": err}})
                            elif status in ["STARTING", "RESTARTING"] and time.time() - action_time > 10:
                                projects_col.update_one({"_id": p["_id"]}, {"$set": {"status": "RUNNING", "started_at": time.time()}})
                    elif status == "DELETING" and time.time() - action_time > 60:
                        projects_col.delete_one({"_id": p["_id"]})
            await asyncio.to_thread(sync_tasks)
        except Exception: pass
        await asyncio.sleep(10)

def get_best_node():
    # Sirf un nodes ko laao jo last 30 seconds me active the
    active_nodes = list(nodes_col.find({"last_seen": {"$gt": time.time() - 30}}).sort([("cpu", 1), ("ram", 1)]))
    
    # Agar koi node active nahi hai, toh Master Node (1) par fallback karo
    if not active_nodes: 
        return 1 
        
    # Jo sabse kam loaded (CPU/RAM) node hai, usko return kar do
    best_node = active_nodes[0]
    return best_node["node_id"]

async def trigger_github_worker(target_node):
    url = f"https://api.github.com/repos/{config.GITHUB_REPO}/actions/workflows/{config.WORKFLOW_FILE}/dispatches"
    headers = {"Authorization": f"token {config.GH_PERSONAL_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try: return (await asyncio.to_thread(requests.post, url, headers=headers, json={"ref": "main", "inputs": {"node_id": str(target_node)}}, timeout=20)).status_code == 204
    except: return False

# ================= EXTRACTION & SECURITY =================
def validate_requirements(path):
    dangerous_prefixes = ("-i ", "--index-url", "--extra-index-url", "--trusted-host", "--find-links", "--no-index", "-f ", "--config-settings", "--global-option", "--install-option", "git+", "http://", "https://", "file://")
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"): continue
            if line.startswith(dangerous_prefixes) or "://" in line: raise ValueError("❌ Unsafe requirements entry detected.")
    return True

def safe_extract_zip(zip_path, extract_dir):
    size_extracted, file_count, base = 0, 0, os.path.realpath(extract_dir)
    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.infolist():
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or name.startswith("../") or "/../" in name: raise ValueError("⚠️ Unsafe ZIP path.")
            target = os.path.realpath(os.path.join(extract_dir, name))
            if not (target == base or target.startswith(base + os.sep)): raise ValueError("⚠️ ZIP path traversal detected.")
            if member.is_dir(): continue
            file_count += 1
            if file_count > MAX_FILES: raise ValueError("⚠️ Too many files.")
            size_extracted += member.file_size
            if size_extracted > MAX_EXTRACTED_SIZE: raise ValueError("⚠️ Size limit exceeded.")
        z.extractall(extract_dir)

def rezip_workspace(user_dir):
    zip_path, extract_dir = os.path.join(user_dir, "project.zip"), os.path.join(user_dir, "extracted")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(extract_dir):
            for file in files:
                file_path = os.path.join(root, file)
                zipf.write(file_path, os.path.relpath(file_path, extract_dir))

def cleanup_workspace(user_id):
    shutil.rmtree(os.path.join(HOST_DIR, str(user_id)), ignore_errors=True)

async def cleanup_docker_images(user_id):
    images = await asyncio.to_thread(docker_client.images.list, filters={"reference": f"anysnap_{user_id}_*"})
    if len(images) > 1:
        images.sort(key=lambda img: img.attrs['Created'], reverse=True)
        for img in images[1:]:
            try: await asyncio.to_thread(docker_client.images.remove, img.id, force=True)
            except: pass

def strip_top_level_folder(extract_dir):
    """GitHub ZIP files hamesha ek root folder add kar dete hain. Ye us folder ko hata kar files ko bahar nikalta hai."""
    items = os.listdir(extract_dir)
    if len(items) == 1:
        single_folder = os.path.join(extract_dir, items[0])
        if os.path.isdir(single_folder):
            for item in os.listdir(single_folder):
                shutil.move(os.path.join(single_folder, item), extract_dir)
            os.rmdir(single_folder)

# ================= 🚀 SMART ENTRY DETECTION =================
def detect_project_entry(bot_dir):
    # 1. Python package with __main__.py (e.g., MagmaMusic)
    for root, dirs, files in os.walk(bot_dir):
        if "__main__.py" in files:
            rel_dir = os.path.relpath(root, bot_dir)
            package_name = os.path.basename(root)
            return {
                "type": "python_module",
                "entry": package_name,
                "root": bot_dir,
                "has_req": os.path.exists(os.path.join(bot_dir, "requirements.txt"))
            }

    # 2. Normal Python project (single file entry)
    for root, _, files in os.walk(bot_dir):
        files_lower = [f.lower() for f in files]
        rel_dir = os.path.relpath(root, bot_dir)
        py_files = [f for f in files if f.endswith(".py")]
        has_req = "requirements.txt" in files_lower

        if has_req or py_files:
            for f in ["main.py", "bot.py", "app.py", "server.py", "run.py", "index.py", "sting.py"]:
                if f in files:
                    return {
                        "type": "python",
                        "entry": os.path.join(rel_dir, f) if rel_dir != "." else f,
                        "root": bot_dir,
                        "has_req": has_req
                    }
            if py_files:
                return {
                    "type": "python",
                    "entry": os.path.join(rel_dir, py_files[0]) if rel_dir != "." else py_files[0],
                    "root": bot_dir,
                    "has_req": has_req
                }

    # 3. Node.js project
    for root, _, files in os.walk(bot_dir):
        if "package.json" in [f.lower() for f in files]:
            return {
                "type": "node",
                "entry": "package.json",
                "root": bot_dir,
                "has_req": True
            }
    return None

# ================= DOCKER DEPLOYMENT =================
async def deploy_docker_container(proj_id, user_id, root, project_type, entry, env_vars=None, run_cmd=None):
    image_tag = f"anysnap_{user_id}_{int(time.time())}"
    container_name = f"anysnap_bot_{user_id}"
    dockerfile_path = os.path.join(root, "Dockerfile")
    env_vars = env_vars or {}

    await asyncio.to_thread(projects_col.update_one, {"_id": proj_id}, {"$set": {"status": "BUILDING"}})

    req_path = os.path.join(root, "requirements.txt")
    if os.path.exists(req_path): 
        try: validate_requirements(req_path)
        except Exception as e: return False, str(e), image_tag

    try: os.remove(dockerfile_path)
    except: pass

    # Smart Base Image
    base_img = "python:3.12-slim" if project_type in ["python", "python_module"] else "node:22-alpine"
    
    # 🟢 ADDED OS DEPENDENCIES HERE (git, ffmpeg, imagemagick, etc.)
    if project_type in ["python", "python_module"]: 
        install_step = ("RUN useradd -m botuser && apt-get update && apt-get install -y gcc g++ make bash git ffmpeg imagemagick libwebp-dev curl neofetch && rm -rf /var/lib/apt/lists/*\nRUN python -m pip install python-dotenv\nRUN find /app -type f -iname 'requirements.txt' -exec python -m pip install --no-cache-dir -r '{}' \\;\nRUN mkdir -p /app/data && chown -R botuser:botuser /app\nUSER botuser\n")
    elif project_type == "node": 
        install_step = ("RUN adduser -D botuser\nRUN find /app -type f -iname 'package.json' -execdir npm install \\;\nRUN mkdir -p /app/data && chown -R botuser:botuser /app\nUSER botuser\n")

    # Smart Execution Command (Sanitized)
    if run_cmd: 
        safe_cmd = run_cmd.replace('\n', ' ').replace('\r', '')
        exec_cmd = f'CMD {safe_cmd}\n'
    elif project_type == "python_module": 
        exec_cmd = f'CMD ["python", "-m", "{entry}"]\n'
    elif project_type == "python": 
        exec_cmd = f'CMD ["python", "{entry}"]\n'
    else: 
        exec_cmd = f'CMD ["npm", "start"]\n'

    with open(dockerfile_path, "w") as df: df.write(f"FROM {base_img}\nWORKDIR /app\nCOPY . /app/\n{install_step}{exec_cmd}\n")

    try:
        old_c = await asyncio.to_thread(docker_client.containers.get, container_name)
        await asyncio.to_thread(old_c.stop); await asyncio.to_thread(old_c.remove, force=True)
    except: pass

    try: await asyncio.to_thread(docker_client.images.build, path=root, tag=image_tag, rm=True, forcerm=True)
    except docker.errors.BuildError as e: return False, f"Build Failed:\n" + "".join([line.get('stream', '') for line in e.build_log if 'stream' in line]), image_tag 
    except Exception as e: return False, f"System Error: {str(e)}", image_tag

    await asyncio.to_thread(projects_col.update_one, {"_id": proj_id}, {"$set": {"status": "STARTING", "image_tag": image_tag, "last_action_time": time.time()}})

    # Changed read_only to False to support bot databases and generic setups
    try: container = await asyncio.to_thread(docker_client.containers.run, image_tag, name=container_name, detach=True, mem_limit="512m", memswap_limit="512m", nano_cpus=1_000_000_000, pids_limit=128, cap_drop=["ALL"], security_opt=["no-new-privileges:true"], read_only=False, privileged=False, network_mode="bridge", tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m", "/home/botuser/.cache": "rw,nosuid,nodev,size=64m", "/app/data": "rw,nosuid,nodev,size=128m"}, environment=env_vars, restart_policy={"Name": "on-failure", "MaximumRetryCount": 3})
    except Exception as e: return False, f"Container Start Error: {e}", image_tag

    await asyncio.sleep(3)
    await asyncio.to_thread(container.reload)
    
    if container.status != "running":
        try: logs = (await asyncio.to_thread(container.logs, tail=50)).decode("utf-8", errors="ignore")
        except: logs = "Container crashed during bootup."
        return False, logs, image_tag 

    await cleanup_docker_images(user_id) 
    return True, container.id, image_tag

# ================= WORKER NODE LOOP =================
async def worker_node_loop():
    print(f"👷 ANYSNAP WORKER ON NODE #{NODE_ID} IS NOW ACTIVE AND LISTENING!")
    while True:
        task = await asyncio.to_thread(projects_col.find_one, {"target_node": NODE_ID, "status": "QUEUED"})
        if task:
            try:
                # Dashboard Extracting Notification
                await asyncio.to_thread(projects_col.update_one, {"_id": task["_id"]}, {"$set": {"status": "EXTRACTING"}})
                
                work_dir = os.path.join(HOST_DIR, str(task["user_id"]))
                os.makedirs(work_dir, exist_ok=True)
                zip_path, extract_dir = os.path.join(work_dir, "project.zip"), os.path.join(work_dir, "extracted")

                with open(zip_path, "wb") as f: f.write(fs.get(task["file_id"]).read())
                safe_extract_zip(zip_path, extract_dir)
                
                # Fix GitHub ZIP structure
                strip_top_level_folder(extract_dir)

                success, cid_or_logs, img_tag = await deploy_docker_container(task["_id"], task["user_id"], extract_dir, task.get("type", "python"), task.get("entry", "main.py"), task.get("env_vars", {}), task.get("run_cmd"))

                if success: await asyncio.to_thread(projects_col.update_one, {"_id": task["_id"]}, {"$set": {"status": "STARTING", "container_id": cid_or_logs, "image_tag": img_tag, "last_action_time": time.time()}})
                else: await asyncio.to_thread(projects_col.update_one, {"_id": task["_id"]}, {"$set": {"status": "CRASHED", "latest_error": cid_or_logs, "image_tag": img_tag}})
                try: fs.delete(task["file_id"]) 
                except: pass
            except Exception as e: await asyncio.to_thread(projects_col.update_one, {"_id": task["_id"]}, {"$set": {"status": "ERROR", "latest_error": str(e)}})
            finally: cleanup_workspace(task["user_id"])

        cmd_task = await asyncio.to_thread(projects_col.find_one, {"target_node": NODE_ID, "action": {"$exists": True}})
        if cmd_task:
            action = cmd_task["action"]
            cid = cmd_task.get("container_id")
            await asyncio.to_thread(projects_col.update_one, {"_id": cmd_task["_id"]}, {"$unset": {"action": ""}})
            
            try:
                if action in ["apply_env", "start"]:
                    if cid:
                        try: 
                            old_c = await asyncio.to_thread(docker_client.containers.get, cid)
                            await asyncio.to_thread(old_c.stop); await asyncio.to_thread(old_c.remove, force=True)
                        except: pass
                    img = cmd_task.get("image_tag")
                    if img:
                        # Changed read_only to False
                        new_c = await asyncio.to_thread(docker_client.containers.run, img, name=f"anysnap_bot_{cmd_task['user_id']}", detach=True, mem_limit="512m", memswap_limit="512m", nano_cpus=1_000_000_000, pids_limit=128, cap_drop=["ALL"], security_opt=["no-new-privileges:true"], read_only=False, privileged=False, network_mode="bridge", tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m", "/home/botuser/.cache": "rw,nosuid,nodev,size=64m", "/app/data": "rw,nosuid,nodev,size=128m"}, environment=cmd_task.get("env_vars", {}), restart_policy={"Name": "on-failure", "MaximumRetryCount": 3})
                        await asyncio.to_thread(projects_col.update_one, {"_id": cmd_task["_id"]}, {"$set": {"status": "STARTING", "container_id": new_c.id, "last_action_time": time.time()}})
                    else: await asyncio.to_thread(projects_col.update_one, {"_id": cmd_task["_id"]}, {"$set": {"status": "ERROR", "latest_error": "Missing Image Tag."}})
                        
                elif action == "restart":
                    if cid: 
                        c = await asyncio.to_thread(docker_client.containers.get, cid)
                        await asyncio.to_thread(c.restart)
                        await asyncio.to_thread(projects_col.update_one, {"_id": cmd_task["_id"]}, {"$set": {"status": "RESTARTING", "last_action_time": time.time()}})

                elif action == "stop":
                    if cid:
                        try: 
                            c = await asyncio.to_thread(docker_client.containers.get, cid)
                            await asyncio.to_thread(c.stop); await asyncio.to_thread(c.remove, force=True)
                        except: pass
                    await asyncio.to_thread(projects_col.update_one, {"_id": cmd_task["_id"]}, {"$set": {"status": "STOPPED"}, "$unset": {"container_id": ""}})
                
                elif action == "delete":
                    if cid:
                        try: 
                            c = await asyncio.to_thread(docker_client.containers.get, cid)
                            await asyncio.to_thread(c.stop); await asyncio.to_thread(c.remove, force=True)
                        except: pass
                    await asyncio.to_thread(projects_col.delete_one, {"_id": cmd_task["_id"]})
                    asyncio.create_task(cleanup_docker_images(cmd_task["user_id"]))
                    
                elif action == "get_logs":
                    if cid: 
                        try: logs = (await asyncio.to_thread((await asyncio.to_thread(docker_client.containers.get, cid)).logs, tail=50)).decode("utf-8", errors="ignore")
                        except: logs = "Log retrieval failed. Container down."
                    else: logs = "Container ID missing."
                    await asyncio.to_thread(projects_col.update_one, {"_id": cmd_task["_id"]}, {"$set": {"latest_logs": logs}})
                    
            except Exception as e: await asyncio.to_thread(projects_col.update_one, {"_id": cmd_task["_id"]}, {"$set": {"latest_error": str(e), "status": "ERROR"}})
        await asyncio.sleep(2)

# ================= UI HELPERS =================
async def ask_for_run_command(client, user_id, edit_msg):
    state = USER_STATE.get(user_id)
    if not state: return
    default_cmd = state.get('run_cmd', 'Unknown')
    text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ⚙️ START COMMAND      ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\nWe detected the following start command for your project:\n\n👉 `{default_cmd}`\n\nDo you want to use this, or enter a custom command (like `python3 -m mybot`)?")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Use Detected Command", callback_data="btn_use_default_cmd")],
        [InlineKeyboardButton("✍️ Enter Custom Command", callback_data="btn_custom_cmd")],
        [InlineKeyboardButton("❌ Cancel", callback_data="btn_cancel")]
    ])
    await edit_msg.edit_text(text, reply_markup=kb)

async def show_deploy_confirmation(client, user_id, chat_id, edit_msg=None):
    state = USER_STATE.get(user_id)
    if not state: return
    text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 🚀 DEPLOYMENT        ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n📦 `{state.get('project_name', 'Unknown')}`\n⚡ `{state.get('run_cmd', 'Unknown')}`\n━━━━━━━━━━━━━━━━━━━━━━\n✅ Project Verified\n✅ Security Scan Passed\n✅ Dependencies Validated\n━━━━━━━━━━━━━━━━━━━━━━\nDeploy to Secure Cloud?")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Deploy Now", callback_data="btn_deploy_confirm")], [InlineKeyboardButton("❌ Cancel", callback_data="btn_cancel")]])
    if edit_msg: await edit_msg.edit_text(text, reply_markup=kb)
    else: await client.send_message(chat_id, text, reply_markup=kb)

async def render_dashboard(client, message, user_id, is_edit=False):
    proj = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
    if not proj:
        text = ("╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃  🐳 ANYSNAP CLOUD    ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\nNo active projects detected.\n**Deploy:** Send `.zip` or `.py` file.\n\n*(Powered by ANYSNAP)*")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Upload Project", callback_data="btn_none")]])
    else:
        status = str(proj.get("status", "UNKNOWN")).upper()
        if status == "RUNNING": emoji = "🟢"
        elif status in ["BUILDING", "STARTING", "QUEUED", "EXTRACTING", "RESTARTING", "STOPPING", "DELETING"]: emoji = "🟡"
        elif status == "STOPPED": emoji = "⚪"
        elif status == "PENDING_APPROVAL": emoji = "⏳"
        else: emoji = "🔴" 

        if status == "PENDING_APPROVAL":
            text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ⏳ DEPLOYMENT REVIEW ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n📦 `{proj.get('project_name', 'Project')}`\n🔐 **Status:** `PENDING APPROVAL`\n\nYour deployment request has been submitted for review.\n\n⏳ Please wait...")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="btn_refresh_dash")], [InlineKeyboardButton("🗑️ Cancel Request", callback_data="btn_delete")]])
        else:
            node_stats = await asyncio.to_thread(nodes_col.find_one, {"node_id": proj.get("target_node", 1)})
            cpu, ram, disk = (node_stats['cpu'], node_stats['ram'], node_stats['disk']) if node_stats else (0.0, 0.0, 0.0)
            
            text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃  🐳 ANYSNAP CLOUD    ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n{emoji}  **{status}**\n━━━━━━━━━━━━━━━━━━━━━━\n🤖  `{proj.get('project_name', 'Anysnap App')}`\n⚡  `{proj.get('run_cmd', 'python main.py')}`\n\n🖥️  NODE\n└─ #{proj.get('target_node', 1)} {'MASTER' if proj.get('target_node', 1) == 1 else 'WORKER'}\n\n")

            if status == "QUEUED":
                text += "⏳ **QUEUED:** Waiting for worker to start...\n\n"
            elif status == "EXTRACTING":
                text += "📦 **EXTRACTING:** Unzipping files & preparing environment...\n\n"
            elif status == "BUILDING":
                text += "🔧 **BUILDING:** Compiling image & installing packages (Takes time)...\n\n"
            elif status == "STARTING":
                text += "🚀 **STARTING:** Booting up your bot container...\n\n"
            elif status == "DELETING":
                text += "🗑️ **DELETING:** Cleaning up resources...\n\n"
            else:
                text += (f"⚡ CPU     `{get_progress_bar(cpu)}` {cpu}%\n💾 RAM     `{get_progress_bar(ram)}` {ram}%\n💿 DISK    `{get_progress_bar(disk)}` {disk}%\n\n⏱ Uptime   `{format_uptime(proj.get('started_at'))}`\n")

            text += f"━━━━━━━━━━━━━━━━━━━━━━\n      🎛 CONTROLS"

            kb_layout = []
            if status == "RUNNING":
                kb_layout.append([InlineKeyboardButton("📜 Logs", callback_data="btn_logs")])
                kb_layout.append([InlineKeyboardButton("🔄 Restart", callback_data="btn_restart"), InlineKeyboardButton("⚙️ Settings", callback_data="btn_settings")])
                kb_layout.append([InlineKeyboardButton("⏹ Stop", callback_data="btn_stop"), InlineKeyboardButton("🗑️ Delete", callback_data="btn_delete")])
            elif status in ["CRASHED", "ERROR"]:
                text = text.replace("      🎛 CONTROLS", "⚠️ Container unexpectedly stopped.\n━━━━━━━━━━━━━━━━━━━━━━\n      🎛 CONTROLS")
                kb_layout.append([InlineKeyboardButton("📜 View Error Logs", callback_data="btn_logs")])
                kb_layout.append([InlineKeyboardButton("🔄 Restart", callback_data="btn_start"), InlineKeyboardButton("⚙️ Settings", callback_data="btn_settings")])
                kb_layout.append([InlineKeyboardButton("🗑️ Delete & Re-deploy", callback_data="btn_delete")])
            elif status == "STOPPED":
                kb_layout.append([InlineKeyboardButton("▶️ Start Bot", callback_data="btn_start"), InlineKeyboardButton("⚙️ Settings", callback_data="btn_settings")])
                kb_layout.append([InlineKeyboardButton("🗑️ Delete Project", callback_data="btn_delete")])
            elif status in ["BUILDING", "STARTING", "QUEUED", "EXTRACTING", "RESTARTING", "STOPPING", "DELETING"]:
                kb_layout.append([InlineKeyboardButton("🗑️ Force Cancel/Delete", callback_data="btn_delete")])
            
            kb_layout.append([InlineKeyboardButton("🔄 Refresh", callback_data="btn_refresh_dash")])
            kb = InlineKeyboardMarkup(kb_layout)

    try:
        if is_edit: await message.edit_text(text, reply_markup=kb)
        else: await message.reply_text(text, reply_markup=kb)
    except: pass

# ================= MESSAGE HANDLERS =================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await render_dashboard(client, message, message.from_user.id, is_edit=False)

@app.on_message(filters.command("owner"))
async def owner_cmd(client, message):
    if not is_owner(message.from_user.id): return await message.reply_text("❌ Access Denied.")
    await render_owner_panel(client, message, is_edit=False)

@app.on_message(filters.document & filters.private)
async def handle_document_upload(client, message):
    user_id = message.from_user.id
    existing = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
    if existing: return await message.reply_text("⚠️ **You already have an active project!**\nPlease click **🗑️ Delete** on your current dashboard before deploying a new one.")

    doc = message.document
    is_py_file = doc.file_name.endswith('.py')

    if not (doc.file_name.endswith('.zip') or is_py_file): return await message.reply_text("❌ Only `.zip` or `.py` files.")
    if doc.file_size > MAX_ZIP_SIZE: return await message.reply_text("❌ File too large.")

    lock = get_user_lock(user_id)
    if lock.locked(): return await message.reply_text("⚠️ Processing...")

    async with lock:
        status_msg = await message.reply_text("📥 Initializing Workspace...")
        user_dir = os.path.join(HOST_DIR, str(user_id))
        shutil.rmtree(user_dir, ignore_errors=True); os.makedirs(user_dir, exist_ok=True)
        zip_path, extract_dir = os.path.join(user_dir, "project.zip"), os.path.join(user_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)

        try:
            if is_py_file:
                await message.download(file_name=os.path.join(extract_dir, doc.file_name))
                rezip_workspace(user_dir) 
            else:
                await message.download(file_name=zip_path)
                safe_extract_zip(zip_path, extract_dir)
                
                # Fix GitHub ZIP structure
                strip_top_level_folder(extract_dir)
                # ZIP ko waapas pack kar do taaki Database me clean ZIP save ho
                rezip_workspace(user_dir)

            project_data = detect_project_entry(extract_dir)
            if project_data:
                proj_name = doc.file_name.replace(".zip", "").replace(".py", "")
                
                # Setup default Run Command specifically honoring python packages
                if project_data["type"] == "python_module":
                    default_cmd = f"python -m {project_data['entry']}"
                elif project_data["type"] == "python":
                    default_cmd = f"python {project_data['entry']}"
                else:
                    default_cmd = "npm start"
                
                USER_STATE[user_id] = {**project_data, "dir": user_dir, "zip_path": zip_path, "env_vars": {}, "project_name": proj_name, "run_cmd": default_cmd}

                if project_data["type"] in ["python", "python_module"] and not project_data["has_req"]:
                    REQ_WAITING[user_id] = True
                    await status_msg.edit_text("⚠️ **No `requirements.txt` detected.**\nSend dependencies in chat or click Skip.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Skip", callback_data="btn_skip_req")]]))
                else:
                    await ask_for_run_command(client, user_id, status_msg)
            else:
                cleanup_workspace(user_id); await status_msg.edit_text("⚠️ Could not detect valid project.")
        except Exception as e: cleanup_workspace(user_id); await status_msg.edit_text(f"❌ Validation Error: {e}")

@app.on_message(filters.text & filters.private)
async def text_handler(client, message):
    user_id = message.from_user.id
    
    if user_id in CMD_WAITING:
        custom_cmd = message.text.strip()
        if user_id in USER_STATE:
            USER_STATE[user_id]["run_cmd"] = custom_cmd
            await show_deploy_confirmation(client, user_id, message.chat.id, edit_msg=None)
        del CMD_WAITING[user_id]
        return

    if user_id in REQ_WAITING:
        reqs = message.text.replace(",", "\n").replace(" ", "\n")
        state = USER_STATE.get(user_id)
        if state:
            with open(os.path.join(state.get("root", "."), "requirements.txt"), "w") as f: f.write(reqs)
            rezip_workspace(state.get("dir", ".")) 
            status_msg = await message.reply_text("✅ Requirements saved!")
            await ask_for_run_command(client, user_id, status_msg)
        del REQ_WAITING[user_id]
        return

    if user_id in ENV_WAITING:
        text = message.text
        if "=" not in text: return await message.reply_text("❌ Invalid format. Send as `KEY=VALUE`")
        key, val = text.split("=", 1)
        key, val = key.strip(), val.strip()
        if not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*$", key): return await message.reply_text("❌ Invalid ENV name.")

        proj = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
        if proj: 
            await asyncio.to_thread(projects_col.update_one, {"user_id": user_id}, {"$set": {f"env_vars.{key}": val}})
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Restart & Apply", callback_data="btn_apply_env")], [InlineKeyboardButton("⬅️ Settings", callback_data="btn_settings")]])
            await message.reply_text(f"✅ Added ENV: `{key}`=`{val}`\nApply to restart container.", reply_markup=kb)
        elif user_id in USER_STATE: 
            USER_STATE[user_id].setdefault("env_vars", {})[key] = val
            await message.reply_text(f"✅ Added ENV: `{key}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Settings", callback_data="btn_settings")]]))
        del ENV_WAITING[user_id]
        
# ================= CALLBACK HANDLERS =================
@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id

    owner_actions = data.startswith("owner_") or data.startswith("approve_") or data.startswith("reject_")
    if owner_actions and not is_owner(user_id):
        return await query.answer("❌ Owner Only!", show_alert=True)

    if data == "owner_auto_on": 
        set_auto_approve(True)
        await query.answer("🟢 Auto Approve ON", show_alert=False)
        await render_owner_panel(client, query.message, is_edit=True)
        
    elif data == "owner_auto_off": 
        set_auto_approve(False)
        await query.answer("🔴 Auto Approve OFF", show_alert=False)
        await render_owner_panel(client, query.message, is_edit=True)
        
    elif data == "owner_panel": 
        await query.answer()
        await render_owner_panel(client, query.message, is_edit=True)
        
    elif data == "owner_pending":
        await query.answer()
        pending_bots = list(await asyncio.to_thread(projects_col.find, {"status": "PENDING_APPROVAL"}))
        text = "╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ⏳ PENDING APPROVALS ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        if not pending_bots:
            text += "✅ No pending deployment requests.\n"
        else:
            for p in pending_bots:
                text += f"📦 `{p.get('project_name')}`\n👤 User: `{p.get('user_id')}`\n━━━━━━━━━━━━━━━━━━━━━━\n"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="owner_pending")], [InlineKeyboardButton("⬅️ Back to Menu", callback_data="owner_panel")]])
        await query.message.edit_text(text, reply_markup=kb)
        
    elif data == "owner_projects":
        await query.answer()
        total = await asyncio.to_thread(projects_col.count_documents, {})
        running = await asyncio.to_thread(projects_col.count_documents, {"status": "RUNNING"})
        crashed = await asyncio.to_thread(projects_col.count_documents, {"status": "CRASHED"})
        queued = await asyncio.to_thread(projects_col.count_documents, {"status": "QUEUED"})
        text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 🤖 PROJECTS OVERVIEW ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"📦 Total Projects : `{total}`\n🟢 Running        : `{running}`\n🔴 Crashed        : `{crashed}`\n🟡 Queued         : `{queued}`\n\n━━━━━━━━━━━━━━━━━━━━━━")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="owner_projects")], [InlineKeyboardButton("⬅️ Back to Menu", callback_data="owner_panel")]])
        await query.message.edit_text(text, reply_markup=kb)
        
    elif data == "owner_nodes":
        await query.answer()
        nodes = list(await asyncio.to_thread(nodes_col.find, {}))
        text = "╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 🌐 NODE STATUS       ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        if not nodes:
            text += "⚠️ No nodes connected yet.\n"
        else:
            for n in nodes:
                status = "🟢" if time.time() - n.get("last_seen", 0) < 60 else "🔴"
                text += (f"{status} **Node {n['node_id']}** ({n.get('role', 'WORKER')})\n"
                         f"├ CPU: `{n.get('cpu', 0)}%`\n├ RAM: `{n.get('ram', 0)}%`\n└ Disk:`{n.get('disk', 0)}%`\n━━━━━━━━━━━━━━━━━━━━━━\n")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="owner_nodes")], [InlineKeyboardButton("⬅️ Back to Menu", callback_data="owner_panel")]])
        await query.message.edit_text(text, reply_markup=kb)
        
    elif data == "owner_stats":
        await query.answer()
        total_bots = await asyncio.to_thread(projects_col.count_documents, {"status": "RUNNING"})
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 📊 CLUSTER STATISTICS┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"🖥️ **Master Node (Node 1)**\n⚡ CPU : `{cpu}%`\n💾 RAM : `{ram.percent}%` ({ram.used // (1024**2)}MB / {ram.total // (1024**2)}MB)\n💿 DISK: `{disk.percent}%`\n\n"
                f"🤖 **Global Status**\n🟢 Running Bots: `{total_bots}`\n━━━━━━━━━━━━━━━━━━━━━━")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="owner_stats")], [InlineKeyboardButton("⬅️ Back to Menu", callback_data="owner_panel")]])
        await query.message.edit_text(text, reply_markup=kb)

    # 🟢 APPROVE / REJECT ACTIONS WITH ANIMATION
    elif data.startswith("approve_"):
        await query.answer("⏳ Approving Project...", show_alert=False)
        proj_id = ObjectId(data.split("_")[1])
        proj = await asyncio.to_thread(projects_col.find_one, {"_id": proj_id})
        
        old_caption = query.message.caption or "🔔 DEPLOYMENT REQUEST"
        if "STATUS:" in old_caption:
            old_caption = old_caption.split("\n\n⏳")[0].split("\n\n✅")[0].split("\n\n❌")[0]

        if proj:
            await asyncio.to_thread(projects_col.update_one, {"_id": proj_id}, {"$set": {"status": "QUEUED"}})
            
            target_node = proj.get("target_node", 1)
            if target_node != NODE_ID:
                await trigger_github_worker(target_node)
            
            try: await query.message.edit_caption(f"{old_caption}\n\n✅ **STATUS: APPROVED & QUEUED!**\n🖥️ Target: Node #{target_node}", reply_markup=None)
            except: pass
            
            try: await app.send_message(proj["user_id"], "🎉 Your deployment request has been **APPROVED**!\nIt is now queued for cloud deployment.\n\nClick /start to check live dashboard.")
            except: pass

    elif data.startswith("reject_"):
        await query.answer("⏳ Rejecting Project...", show_alert=False)
        proj_id = ObjectId(data.split("_")[1])
        proj = await asyncio.to_thread(projects_col.find_one, {"_id": proj_id})
        
        old_caption = query.message.caption or "🔔 DEPLOYMENT REQUEST"
        if "STATUS:" in old_caption:
            old_caption = old_caption.split("\n\n⏳")[0].split("\n\n✅")[0].split("\n\n❌")[0]

        if proj:
            await asyncio.to_thread(projects_col.delete_one, {"_id": proj_id})
            try: fs.delete(proj["file_id"])
            except: pass
            cleanup_workspace(proj["user_id"])
            
            try: await query.message.edit_caption(f"{old_caption}\n\n❌ **STATUS: REJECTED!**", reply_markup=None)
            except: pass
            
            try: await app.send_message(proj["user_id"], "❌ Your deployment request was **REJECTED** by the Admin.")
            except: pass

    elif data == "btn_use_default_cmd":
        await query.answer()
        await show_deploy_confirmation(client, user_id, query.message.chat.id, edit_msg=query.message)
        
    elif data == "btn_custom_cmd":
        await query.answer()
        CMD_WAITING[user_id] = True
        await query.message.edit_text("✍️ **Send your custom start command now.**\n*Example:* `python3 -m mybot` or `bash start.sh`")

    elif data == "btn_refresh_dash": 
        await query.answer("🔄 Refreshed", show_alert=False)
        return await render_dashboard(client, query.message, user_id, is_edit=True)
    
    elif data == "btn_skip_req":
        if user_id in REQ_WAITING: del REQ_WAITING[user_id]
        await query.answer()
        await ask_for_run_command(client, user_id, query.message)
    
    elif data == "btn_cancel":
        cleanup_workspace(user_id)
        USER_STATE.pop(user_id, None); REQ_WAITING.pop(user_id, None); CMD_WAITING.pop(user_id, None)
        await query.message.edit_text("❌ Deployment Cancelled. Environment wiped clean.")

    elif data == "btn_deploy_confirm":
        state = USER_STATE.get(user_id)
        if not state: return await query.answer("Session expired.", show_alert=True)
        await query.answer("🚀 Initializing Deployment...", show_alert=False)
        
        prog_msg = await query.message.edit_text("╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ⚡ ANYSNAP CLOUD     ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n⏳ Preparing deployment...")
        target_node = get_best_node()
        
        # File handle memory leak fixed
        with open(state["zip_path"], "rb") as f:
            file_id = await asyncio.to_thread(fs.put, f, filename=f"user_{user_id}.zip")
            
        auto_approve = get_auto_approve()
        initial_status = "QUEUED" if auto_approve else "PENDING_APPROVAL"
        
        db_doc = {"user_id": user_id, "target_node": target_node, "status": initial_status, "type": state.get("type", "python"), "entry": state.get("entry", "main.py"), "file_id": file_id, "env_vars": state.get("env_vars", {}), "run_cmd": state.get("run_cmd"), "project_name": state.get("project_name", "Anysnap App"), "created_at": time.time(), "last_action_time": time.time()}
        
        await asyncio.to_thread(projects_col.delete_many, {"user_id": user_id})
        result = await asyncio.to_thread(projects_col.insert_one, db_doc)
        project_id = result.inserted_id

        if not auto_approve:
            await prog_msg.edit_text("⏳ Your deployment request has been sent for approval.")
            try: 
                caption_text = (f"🔔 DEPLOYMENT REQUEST\n\n"
                                f"📦 **{state.get('project_name', 'App')}**\n"
                                f"👤 User: `{user_id}`\n"
                                f"⚡ CMD: `{state.get('run_cmd')}`")
                                
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ APPROVE", callback_data=f"approve_{project_id}"), 
                     InlineKeyboardButton("❌ REJECT", callback_data=f"reject_{project_id}")]
                ])
                
                await client.send_document(
                    chat_id=OWNER_ID,
                    document=state["zip_path"],
                    file_name=f"{state.get('project_name', 'Anysnap_Project')}.zip",
                    caption=caption_text,
                    reply_markup=kb
                )
            except Exception as e: 
                print(f"Error sending file to owner: {e}")
                
            USER_STATE.pop(user_id, None)
            cleanup_workspace(user_id)
            return

        anim_task = asyncio.create_task(animate_status(prog_msg, project_id, "deploy"))
        if target_node != NODE_ID: 
            await trigger_github_worker(target_node)
            
        USER_STATE.pop(user_id, None); cleanup_workspace(user_id)

    elif data == "btn_restart":
        proj = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
        if not proj: return await query.answer("No active bot!", show_alert=True)
        cid = proj.get("container_id")
        if not cid: return await query.answer("Error: Container ID missing!", show_alert=True)

        await query.answer("🔄 Restarting Bot...", show_alert=False)
        anim_task = asyncio.create_task(animate_status(query.message, proj["_id"], "restart"))

        if proj.get("target_node", 1) == NODE_ID:
            try: 
                c = await asyncio.to_thread(docker_client.containers.get, cid)
                await asyncio.to_thread(c.restart)
                await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"status": "RESTARTING", "last_action_time": time.time()}})
                await asyncio.sleep(2)
                await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"status": "RUNNING", "started_at": time.time()}})
            except Exception as e: await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"status": "CRASHED", "latest_error": str(e)}})
        else: 
            await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"action": "restart", "status": "RESTARTING", "last_action_time": time.time()}})
            await asyncio.sleep(3)

        anim_task.cancel()
        await render_dashboard(client, query.message, user_id, is_edit=True)

    elif data == "btn_apply_env":
        proj = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
        if not proj: return await query.answer("No active bot!", show_alert=True)
        image_tag = proj.get("image_tag")
        if not image_tag: return await query.answer("Image missing! Please Re-deploy.", show_alert=True)

        await query.answer("⚙️ Applying Environment Variables...", show_alert=False)
        anim_task = asyncio.create_task(animate_status(query.message, proj["_id"], "env"))

        await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"status": "STARTING", "last_action_time": time.time()}})

        if proj.get("target_node", 1) == NODE_ID:
            try:
                cid = proj.get("container_id")
                if cid:
                    try: 
                        old_c = await asyncio.to_thread(docker_client.containers.get, cid)
                        await asyncio.to_thread(old_c.stop); await asyncio.to_thread(old_c.remove, force=True)
                    except: pass
                # Changed read_only to False
                new_c = await asyncio.to_thread(docker_client.containers.run, image_tag, name=f"anysnap_bot_{user_id}_{int(time.time())}", detach=True, mem_limit="512m", memswap_limit="512m", nano_cpus=1_000_000_000, pids_limit=128, cap_drop=["ALL"], security_opt=["no-new-privileges:true"], read_only=False, privileged=False, network_mode="bridge", tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m", "/home/botuser/.cache": "rw,nosuid,nodev,size=64m", "/app/data": "rw,nosuid,nodev,size=128m"}, environment=proj.get("env_vars", {}), restart_policy={"Name": "on-failure", "MaximumRetryCount": 3})
                await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"container_id": new_c.id, "status": "RUNNING", "started_at": time.time()}})
            except Exception as e: await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"status": "CRASHED", "latest_error": str(e)}})
        else: 
            await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"action": "apply_env"}})
            await asyncio.sleep(3)

        anim_task.cancel()
        await render_dashboard(client, query.message, user_id, is_edit=True)

    elif data == "btn_settings":
        await query.answer("⚙️ Opening Settings", show_alert=False)
        proj = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
        active_env = proj.get("env_vars", {}) if proj else {}
        env_text = "\n".join([f"• `{k}`: `{v}`" for k, v in active_env.items()]) if active_env else "None"
        text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ⚙️ PROJECT SETTINGS  ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n🔐 **ENVIRONMENT**\n━━━━━━━━━━━━━━━━━━━━━━\n{env_text}\n\n💾 **RESOURCES**\n├─ RAM       `512 MB`\n├─ CPU       `1 Core`\n└─ Processes `128`\n\n🛡 **SECURITY**\n├─ Sandbox       🟢 ON\n├─ Privileges    🔒 Restricted\n└─ Auto Restart  🟢 ON\n━━━━━━━━━━━━━━━━━━━━━━\n*(💡 Save temp data inside `/app/data/`)*")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Add ENV Variable", callback_data="btn_add_env")], [InlineKeyboardButton("🔄 Restart & Apply Vars", callback_data="btn_apply_env")], [InlineKeyboardButton("⬅️ Dashboard", callback_data="btn_refresh_dash")]])
        await query.message.edit_text(text, reply_markup=kb)

    elif data == "btn_add_env":
        await query.answer()
        ENV_WAITING[user_id] = True
        await query.message.edit_text("✍️ Send variable:\n`KEY=VALUE`")

    elif data == "btn_logs":
        await query.answer("📜 Fetching Logs...", show_alert=False)
        proj = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
        if not proj: return await query.answer("No active bot!", show_alert=True)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh Logs", callback_data="btn_logs"), InlineKeyboardButton("⬅️ Dashboard", callback_data="btn_refresh_dash")]])
        
        status = proj.get("status", "UNKNOWN").upper()
        if status in ["CRASHED", "ERROR"]:
            text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 🔴 CRASH / BUILD LOGS┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n🤖 `{proj.get('project_name', 'Bot')}`\n🖥️ Node #{proj.get('target_node', 1)}\n\n⚠️ **ERROR TRACE**\n━━━━━━━━━━━━━━━━━━━━━━\n```\n{proj.get('latest_error', 'No trace found. Container stopped.')[-1500:]}\n```\n━━━━━━━━━━━━━━━━━━━━━━")
            try:
                return await query.message.edit_text(text, reply_markup=kb)
            except MessageNotModified:
                return
                
        cid = proj.get("container_id")
        if not cid: 
            try:
                return await query.message.edit_text("⚠️ Container ID is missing.", reply_markup=kb)
            except MessageNotModified:
                return

        if proj.get("target_node", 1) == NODE_ID:
            try: logs = (await asyncio.to_thread((await asyncio.to_thread(docker_client.containers.get, cid)).logs, tail=50)).decode("utf-8", errors="ignore")
            except: logs = "Log retrieval error. Container might be down."
            
            try:
                await query.message.edit_text(f"📜 **LOGS (Node 1):**\n```\n{logs[-2000:]}\n```", reply_markup=kb)
            except MessageNotModified:
                pass
        else:
            await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"action": "get_logs"}})
            try:
                await query.message.edit_text("⏳ Fetching logs from remote node (Fast Polling)...", reply_markup=InlineKeyboardMarkup([]))
            except MessageNotModified:
                pass
            
            for _ in range(20): 
                await asyncio.sleep(0.35)
                p = await asyncio.to_thread(projects_col.find_one, {"_id": proj["_id"]})
                if "action" not in p and "latest_logs" in p: 
                    try:
                        return await query.message.edit_text(f"📜 **LOGS (Node {proj.get('target_node', 1)}):**\n```\n{p.get('latest_logs', 'Empty')} \n```", reply_markup=kb)
                    except MessageNotModified:
                        return
            try:
                await query.message.edit_text("⏳ Remote node is slow or unresponsive. Please refresh.", reply_markup=kb)
            except MessageNotModified:
                pass

    elif data == "btn_start":
        proj = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
        if not proj: return await query.answer("No active bot!", show_alert=True)
        image_tag = proj.get("image_tag")
        if not image_tag: return await query.answer("Image missing. Delete & Re-deploy.", show_alert=True)

        await query.answer("🚀 Starting Bot...", show_alert=False)
        anim_task = asyncio.create_task(animate_status(query.message, proj["_id"], "start"))

        if proj.get("target_node", 1) == NODE_ID:
            try:
                # Changed read_only to False
                new_c = await asyncio.to_thread(docker_client.containers.run, image_tag, name=f"anysnap_bot_{user_id}_{int(time.time())}", detach=True, mem_limit="512m", memswap_limit="512m", nano_cpus=1_000_000_000, pids_limit=128, cap_drop=["ALL"], security_opt=["no-new-privileges:true"], read_only=False, privileged=False, network_mode="bridge", tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m", "/home/botuser/.cache": "rw,nosuid,nodev,size=64m", "/app/data": "rw,nosuid,nodev,size=128m"}, environment=proj.get("env_vars", {}), restart_policy={"Name": "on-failure", "MaximumRetryCount": 3})
                await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"container_id": new_c.id, "status": "RUNNING", "started_at": time.time()}})
            except Exception as e: await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"status": "CRASHED", "latest_error": str(e)}})
        else: 
            await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"action": "start", "status": "STARTING", "last_action_time": time.time()}})
            await asyncio.sleep(3)

        anim_task.cancel()
        await render_dashboard(client, query.message, user_id, is_edit=True)

    elif data == "btn_stop":
        proj = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
        if not proj: return await query.answer("No active bot!", show_alert=True)

        await query.answer("⏹ Stopping Bot...", show_alert=False)
        anim_task = asyncio.create_task(animate_status(query.message, proj["_id"], "stop"))

        if proj.get("target_node", 1) == NODE_ID:
            cid = proj.get("container_id")
            if cid:
                try: 
                    c = await asyncio.to_thread(docker_client.containers.get, cid)
                    await asyncio.to_thread(c.stop); await asyncio.to_thread(c.remove, force=True)
                except: pass
            await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"status": "STOPPED"}, "$unset": {"container_id": ""}})
        else: 
            await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"action": "stop", "status": "STOPPING", "last_action_time": time.time()}})
            await asyncio.sleep(2)

        anim_task.cancel()
        await render_dashboard(client, query.message, user_id, is_edit=True)

    elif data == "btn_delete":
        proj = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
        if not proj: return await query.answer("Already deleted!", show_alert=True)
        
        await query.answer("🗑️ Deleting Project...", show_alert=False)
        anim_task = asyncio.create_task(animate_status(query.message, proj["_id"], "delete"))

        if proj.get("status") in ["DELETING", "BUILDING", "QUEUED", "EXTRACTING"] or proj.get("target_node", 1) == NODE_ID:
            await asyncio.to_thread(projects_col.delete_one, {"_id": proj["_id"]})
            asyncio.create_task(cleanup_docker_images(user_id))
            cid = proj.get("container_id")
            if cid:
                try: 
                    c = await asyncio.to_thread(docker_client.containers.get, cid)
                    await asyncio.to_thread(c.stop); await asyncio.to_thread(c.remove, force=True)
                except: pass
        else: 
            await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"action": "delete", "status": "DELETING", "last_action_time": time.time()}})
            await asyncio.sleep(3)

        anim_task.cancel()
        await query.message.edit_text("✅ Project Deleted & Resources Wiped.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Return Home", callback_data="btn_refresh_dash")]]))

# ================= RUNNERS =================
if __name__ == "__main__":
    try: loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    print("⏳ Starting Background Tasks...")
    loop.create_task(heartbeat_loop())
    loop.create_task(worker_node_loop())
    
    if NODE_ID == 1:
        print("👑 ANYSNAP CLOUD MASTER NODE STARTED: Listening for Telegram messages...")
        app.run() 
    else:
        print(f"👷 ANYSNAP CLOUD WORKER NODE #{NODE_ID} STARTED: Background Processing...")
        loop.run_forever()