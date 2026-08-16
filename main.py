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
USER_STATE, USER_LOCKS, ENV_WAITING, REQ_WAITING = {}, {}, {}, {}

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

# ================= UI HELPERS =================
def format_uptime(started_at):
    if not started_at: return "N/A"
    elapsed = int(time.time() - started_at)
    return f"{elapsed // 3600}h {(elapsed % 3600) // 60}m"

def get_progress_bar(percent):
    filled = int((percent / 100) * 6)
    return ("█" * filled) + ("░" * (6 - filled))

# ================= MAGMA ANIMATION HELPERS =================
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

            text = (
                "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
                "┃ ⚡ MAGMA CLOUD       ┃\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"{spin} **{operation.upper()}**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔹 {step}\n\n"
                "🟢 Cloud Engine\n"
                "🟢 Docker Engine\n"
                "🟢 Database\n"
                "🟡 Operation Running\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "✨ Please wait..."
            )
            await message.edit_text(text)
            i += 1
            await asyncio.sleep(0.8)
        except Exception:
            await asyncio.sleep(0.8)

# ================= RECONCILIATION ENGINE (HEARTBEAT) =================
async def heartbeat_loop():
    while True:
        try:
            def sync_tasks():
                local_containers = docker_client.containers.list(filters={"name": "anysnap_"}, all=True)
                nodes_col.update_one(
                    {"node_id": NODE_ID}, 
                    {"$set": {
                        "role": "MASTER" if NODE_ID == 1 else "WORKER", 
                        "cpu": psutil.cpu_percent(interval=None), 
                        "ram": psutil.virtual_memory().percent, 
                        "disk": psutil.disk_usage('/').percent, 
                        "containers": sum(1 for c in local_containers if c.status == "running"), 
                        "last_seen": time.time()
                    }}, 
                    upsert=True
                )
                
                active_cids = {c.id: c for c in local_containers}
                local_projects = projects_col.find({"target_node": NODE_ID})
                
                for p in local_projects:
                    status = p.get("status")
                    cid = p.get("container_id")
                    action_time = p.get("last_action_time", 0)

                    if status in ["RUNNING", "STARTING", "RESTARTING"]:
                        if not cid or cid not in active_cids:
                            projects_col.update_one({"_id": p["_id"]}, {"$set": {"status": "CRASHED", "latest_error": "Container stopped unexpectedly."}})
                        else:
                            container = active_cids[cid]
                            if container.status != "running":
                                try: err = container.logs(tail=50).decode("utf-8", errors="ignore")
                                except: err = "Container crashed. No logs available."
                                projects_col.update_one({"_id": p["_id"]}, {"$set": {"status": "CRASHED", "latest_error": err}})
                            elif status in ["STARTING", "RESTARTING"]:
                                if time.time() - action_time > 10:
                                    projects_col.update_one({"_id": p["_id"]}, {"$set": {"status": "RUNNING", "started_at": time.time()}})

                    elif status == "DELETING":
                        if time.time() - action_time > 60:
                            projects_col.delete_one({"_id": p["_id"]})
                            
            await asyncio.to_thread(sync_tasks)
        except Exception: 
            pass
        await asyncio.sleep(10)

def get_best_node():
    active_nodes = list(nodes_col.find({"last_seen": {"$gt": time.time() - 30}}).sort([("cpu", 1), ("ram", 1)]))
    active_ids = [n["node_id"] for n in active_nodes]
    if not active_nodes: return 1 
    best_node = active_nodes[0]
    if best_node.get("cpu", 0) > 85.0 or best_node.get("ram", 0) > 85.0:
        for i in range(1, MAX_NODES + 1):
            if i not in active_ids: return i
        return best_node["node_id"] 
    return best_node["node_id"]

async def trigger_github_worker(target_node):
    url = f"https://api.github.com/repos/{config.GITHUB_REPO}/actions/workflows/{config.WORKFLOW_FILE}/dispatches"
    headers = {"Authorization": f"token {config.GH_PERSONAL_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        res = await asyncio.to_thread(requests.post, url, headers=headers, json={"ref": "main", "inputs": {"node_id": str(target_node)}}, timeout=20)
        return res.status_code == 204
    except: return False

# ================= SECURITY & ZIP EXTRACTION =================
def validate_requirements(path):
    dangerous_prefixes = ("-i ", "--index-url", "--extra-index-url", "--trusted-host", "--find-links", "--no-index", "-f ", "--config-settings", "--global-option", "--install-option", "git+", "http://", "https://", "file://")
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"): continue
            if line.startswith(dangerous_prefixes): raise ValueError("❌ Unsafe requirements entry.")
            if "://" in line: raise ValueError("❌ External URL dependencies forbidden.")
    return True

def safe_extract_zip(zip_path, extract_dir):
    size_extracted, file_count = 0, 0
    base = os.path.realpath(extract_dir)
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

# ================= HARDENED DOCKER DEPLOYMENT =================
async def deploy_docker_container(proj_id, user_id, root, project_type, entry, env_vars=None):
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

    base_img = "python:3.12-slim" if project_type == "python" else "node:22-alpine"
    if project_type == "python": 
        install_step = (
            "RUN useradd -m botuser && apt-get update && apt-get install -y gcc g++ make && rm -rf /var/lib/apt/lists/*\n"
            "RUN python -m pip install python-dotenv\n"
            "RUN find /app -type f -iname 'requirements.txt' -exec python -m pip install --no-cache-dir -r '{}' \\;\n"
            "RUN mkdir -p /app/data && chown -R botuser:botuser /app\n"
            "USER botuser\n"
        )
        exec_cmd = f'CMD ["python", "{entry}"]'
    elif project_type == "node": 
        install_step = (
            "RUN adduser -D botuser\n"
            "RUN find /app -type f -iname 'package.json' -execdir npm install \\;\n"
            "RUN mkdir -p /app/data && chown -R botuser:botuser /app\n"
            "USER botuser\n"
        )
        exec_cmd = f'CMD ["npm", "start"]'

    with open(dockerfile_path, "w") as df: 
        df.write(f"FROM {base_img}\nWORKDIR /app\nCOPY . /app/\n{install_step}{exec_cmd}\n")

    try:
        old_c = await asyncio.to_thread(docker_client.containers.get, container_name)
        await asyncio.to_thread(old_c.stop); await asyncio.to_thread(old_c.remove, force=True)
    except: pass

    try:
        await asyncio.to_thread(docker_client.images.build, path=root, tag=image_tag, rm=True, forcerm=True)
    except docker.errors.BuildError as e:
        err_log = "".join([line.get('stream', '') for line in e.build_log if 'stream' in line])
        return False, f"Build Failed:\n{err_log}", image_tag 
    except Exception as e:
        return False, f"System Error: {str(e)}", image_tag

    await asyncio.to_thread(projects_col.update_one, {"_id": proj_id}, {"$set": {"status": "STARTING", "image_tag": image_tag, "last_action_time": time.time()}})

    try:
        container = await asyncio.to_thread(
            docker_client.containers.run, image_tag, name=container_name, detach=True, 
            mem_limit="512m", memswap_limit="512m", nano_cpus=1_000_000_000, pids_limit=128,
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"], read_only=True, privileged=False, network_mode="bridge",
            tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m", "/home/botuser/.cache": "rw,nosuid,nodev,size=64m", "/app/data": "rw,nosuid,nodev,size=128m"}, 
            environment=env_vars, restart_policy={"Name": "on-failure", "MaximumRetryCount": 3}
        )
    except Exception as e:
        return False, f"Container Start Error: {e}", image_tag

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
    print(f"👷 ANYSNAP CLOUD WORKER NODE #{NODE_ID} ACTIVE!")
    while True:
        task = await asyncio.to_thread(projects_col.find_one, {"target_node": NODE_ID, "status": "QUEUED"})
        if task:
            try:
                work_dir = os.path.join(HOST_DIR, str(task["user_id"]))
                os.makedirs(work_dir, exist_ok=True)
                zip_path, extract_dir = os.path.join(work_dir, "project.zip"), os.path.join(work_dir, "extracted")

                with open(zip_path, "wb") as f: f.write(fs.get(task["file_id"]).read())
                safe_extract_zip(zip_path, extract_dir)

                success, cid_or_logs, img_tag = await deploy_docker_container(task["_id"], task["user_id"], extract_dir, task.get("type", "python"), task.get("entry", "main.py"), task.get("env_vars", {}))

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
                        new_c = await asyncio.to_thread(docker_client.containers.run, img, name=f"anysnap_bot_{cmd_task['user_id']}", detach=True, mem_limit="512m", memswap_limit="512m", nano_cpus=1_000_000_000, pids_limit=128, cap_drop=["ALL"], security_opt=["no-new-privileges:true"], read_only=True, privileged=False, network_mode="bridge", tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m", "/home/botuser/.cache": "rw,nosuid,nodev,size=64m", "/app/data": "rw,nosuid,nodev,size=128m"}, environment=cmd_task.get("env_vars", {}), restart_policy={"Name": "on-failure", "MaximumRetryCount": 3})
                        await asyncio.to_thread(projects_col.update_one, {"_id": cmd_task["_id"]}, {"$set": {"status": "STARTING", "container_id": new_c.id, "last_action_time": time.time()}})
                    else:
                        await asyncio.to_thread(projects_col.update_one, {"_id": cmd_task["_id"]}, {"$set": {"status": "ERROR", "latest_error": "Missing Image Tag."}})
                        
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
                        try: 
                            c = await asyncio.to_thread(docker_client.containers.get, cid)
                            logs = (await asyncio.to_thread(c.logs, tail=50)).decode("utf-8", errors="ignore")
                        except: logs = "Log retrieval failed. Container down."
                    else: logs = "Container ID missing."
                    await asyncio.to_thread(projects_col.update_one, {"_id": cmd_task["_id"]}, {"$set": {"latest_logs": logs}})
                    
            except Exception as e: 
                await asyncio.to_thread(projects_col.update_one, {"_id": cmd_task["_id"]}, {"$set": {"latest_error": str(e), "status": "ERROR"}})
        
        await asyncio.sleep(2)

# ================= UI & UTILS =================
def detect_project_entry(bot_dir):
    for root, _, files in os.walk(bot_dir):
        files_lower, rel_dir = [f.lower() for f in files], os.path.relpath(root, bot_dir)
        py_files = [f for f in files if f.endswith('.py')]
        has_req = "requirements.txt" in files_lower

        if has_req or py_files:
            for f in ["main.py", "bot.py", "app.py", "server.py", "run.py", "sting.py", "index.py"]:
                if f in files: return {"type": "python", "entry": os.path.join(rel_dir, f) if rel_dir != "." else f, "root": bot_dir, "has_req": has_req}
            if py_files: return {"type": "python", "entry": os.path.join(rel_dir, py_files[0]) if rel_dir != "." else py_files[0], "root": bot_dir, "has_req": has_req}
        if "package.json" in files_lower: return {"type": "node", "entry": "package.json", "root": bot_dir, "has_req": True}
    return None

async def show_deploy_confirmation(client, user_id, chat_id, edit_msg=None):
    state = USER_STATE.get(user_id)
    if not state: return
    text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 🚀 DEPLOYMENT        ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"📦 `{state.get('project_name', 'Unknown')}`\n"
            f"🐍 `{str(state.get('type', 'Unknown')).upper()} • {state.get('entry', 'Unknown')}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Project Verified\n"
            f"✅ Security Scan Passed\n"
            f"✅ Dependencies Validated\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Deploy to Secure Cloud?")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Deploy Now", callback_data="btn_deploy_confirm")], [InlineKeyboardButton("❌ Cancel", callback_data="btn_cancel")]])
    if edit_msg: await edit_msg.edit_text(text, reply_markup=kb)
    else: await client.send_message(chat_id, text, reply_markup=kb)

# ================= MAGMA STATE-BASED UI DASHBOARD =================
async def render_dashboard(client, message, user_id, is_edit=False):
    proj = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
    if not proj:
        text = ("╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
                "┃  🐳 MAGMA CLOUD      ┃\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                "No active projects detected.\n"
                "**Deploy:** Send `.zip` or `.py` file.\n\n"
                "*(Powered by MAGMA)*")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Upload Project", callback_data="btn_none")]])
    else:
        status = str(proj.get("status", "UNKNOWN")).upper()

        if status == "RUNNING": emoji = "🟢"
        elif status in ["BUILDING", "STARTING", "QUEUED", "RESTARTING", "STOPPING", "DELETING"]: emoji = "🟡"
        elif status == "STOPPED": emoji = "⚪"
        elif status == "PENDING_APPROVAL": emoji = "⏳"
        else: emoji = "🔴" 

        if status == "PENDING_APPROVAL":
            text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
                    f"┃ ⏳ DEPLOYMENT REVIEW ┃\n"
                    f"╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                    f"📦 `{proj.get('project_name', 'Project')}`\n"
                    f"🐍 `{str(proj.get('type', 'Unknown')).upper()}`\n\n"
                    f"🔐 **Status:** `PENDING APPROVAL`\n\n"
                    f"Your deployment request has been submitted for review.\n\n"
                    f"⏳ Please wait...")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="btn_refresh_dash")], [InlineKeyboardButton("🗑️ Cancel Request", callback_data="btn_delete")]])
        else:
            node_stats = await asyncio.to_thread(nodes_col.find_one, {"node_id": proj.get("target_node", 1)})
            cpu, ram, disk = (node_stats['cpu'], node_stats['ram'], node_stats['disk']) if node_stats else (0.0, 0.0, 0.0)
            
            text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
                    f"┃  🐳 MAGMA CLOUD      ┃\n"
                    f"╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                    f"{emoji}  **{status}**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🤖  `{proj.get('project_name', 'Magma App')}`\n"
                    f"🐍  `{str(proj.get('type', 'UNKNOWN')).upper()} • {proj.get('entry', 'main.py')}`\n\n"
                    f"🖥️  NODE\n"
                    f"└─ #{proj.get('target_node', 1)} {'MASTER' if proj.get('target_node', 1) == 1 else 'WORKER'}\n\n")

            if status not in ["BUILDING", "QUEUED", "DELETING"]:
                text += (f"⚡ CPU     `{get_progress_bar(cpu)}` {cpu}%\n"
                         f"💾 RAM     `{get_progress_bar(ram)}` {ram}%\n"
                         f"💿 DISK    `{get_progress_bar(disk)}` {disk}%\n\n"
                         f"⏱ Uptime   `{format_uptime(proj.get('started_at'))}`\n")
            else:
                text += f"⚙️ Processing on backend... Please wait.\n\n"

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
            elif status in ["BUILDING", "STARTING", "QUEUED", "RESTARTING", "STOPPING", "DELETING"]:
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

@app.on_message(filters.command("stats"))
async def stats_cmd(client, message):
    total_bots = await asyncio.to_thread(projects_col.count_documents, {"status": "RUNNING"})
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 📊 CLUSTER STATISTICS ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n🖥️ **Master Node (Node 1)**\n⚡ CPU: `{cpu}%`\n💾 RAM: `{ram.percent}%` ({ram.used // (1024**2)}MB / {ram.total // (1024**2)}MB)\n💿 DISK: `{disk.percent}%`\n\n🤖 **Global Status**\n🟢 Running Bots: `{total_bots}`\n━━━━━━━━━━━━━━━━━━━━━━")
    await message.reply_text(text)

@app.on_message(filters.document & filters.private)
async def handle_document_upload(client, message):
    user_id = message.from_user.id
    existing = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
    if existing:
        return await message.reply_text("⚠️ **You already have an active project!**\nPlease click **🗑️ Delete** on your current dashboard before deploying a new one.")

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

            project_data = detect_project_entry(extract_dir)
            if project_data:
                proj_name = doc.file_name.replace(".zip", "").replace(".py", "")
                USER_STATE[user_id] = {**project_data, "dir": user_dir, "zip_path": zip_path, "env_vars": {}, "project_name": proj_name}

                if project_data["type"] == "python" and not project_data["has_req"]:
                    REQ_WAITING[user_id] = True
                    await status_msg.edit_text("⚠️ **No `requirements.txt` detected.**\nSend dependencies in chat or click Skip.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Skip", callback_data="btn_skip_req")]]))
                else:
                    await show_deploy_confirmation(client, user_id, message.chat.id, edit_msg=status_msg)
            else:
                cleanup_workspace(user_id); await status_msg.edit_text("⚠️ Could not detect valid project.")
        except Exception as e:
            cleanup_workspace(user_id); await status_msg.edit_text(f"❌ Validation Error: {e}")

@app.on_message(filters.text & filters.private)
async def text_handler(client, message):
    user_id = message.from_user.id
    if user_id in REQ_WAITING:
        reqs = message.text.replace(",", "\n").replace(" ", "\n")
        state = USER_STATE.get(user_id)
        if state:
            with open(os.path.join(state.get("root", "."), "requirements.txt"), "w") as f: f.write(reqs)
            rezip_workspace(state.get("dir", ".")) 
            await show_deploy_confirmation(client, user_id, message.chat.id, edit_msg=None)
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
        
# ================= CALLBACK HANDLERS (FAST & ANIMATED) =================
@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id

    if data == "btn_refresh_dash": 
        await query.answer("🔄 Refreshed", show_alert=False)
        return await render_dashboard(client, query.message, user_id, is_edit=True)
    
    elif data == "btn_skip_req":
        if user_id in REQ_WAITING: del REQ_WAITING[user_id]
        await show_deploy_confirmation(client, user_id, query.message.chat.id, edit_msg=query.message)
    
    elif data == "btn_cancel":
        cleanup_workspace(user_id)
        if user_id in USER_STATE: del USER_STATE[user_id]
        if user_id in REQ_WAITING: del REQ_WAITING[user_id]
        await query.message.edit_text("❌ Deployment Cancelled. Environment wiped clean.")

    elif data == "btn_deploy_confirm":
        state = USER_STATE.get(user_id)
        if not state: return await query.answer("Session expired.", show_alert=True)
        await query.answer("🚀 Initializing Deployment...", show_alert=False)
        
        prog_msg = await query.message.edit_text("╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ⚡ MAGMA CLOUD       ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n⏳ Preparing deployment...")
        target_node = get_best_node()
        file_id = await asyncio.to_thread(fs.put, open(state["zip_path"], "rb"), filename=f"user_{user_id}.zip")
        auto_approve = get_auto_approve()
        initial_status = "QUEUED" if auto_approve else "PENDING_APPROVAL"
        
        db_doc = {"user_id": user_id, "target_node": target_node, "status": initial_status, "type": state.get("type", "python"), "entry": state.get("entry", "main.py"), "file_id": file_id, "env_vars": state.get("env_vars", {}), "project_name": state.get("project_name", "Magma App"), "created_at": time.time(), "last_action_time": time.time()}
        
        await asyncio.to_thread(projects_col.delete_many, {"user_id": user_id})
        result = await asyncio.to_thread(projects_col.insert_one, db_doc)
        project_id = result.inserted_id

        if not auto_approve:
            await prog_msg.edit_text("⏳ Your deployment request has been sent for approval.")
            try: await app.send_message(OWNER_ID, f"🔔 DEPLOYMENT REQUEST\n\n📦 **{state.get('project_name', 'App')}**\n👤 User: `{user_id}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ APPROVE", callback_data=f"approve_{project_id}"), InlineKeyboardButton("❌ REJECT", callback_data=f"reject_{project_id}")]]))
            except Exception: pass
            USER_STATE.pop(user_id, None); cleanup_workspace(user_id); return

        # Start background animation for Deploy
        anim_task = asyncio.create_task(animate_status(prog_msg, project_id, "deploy"))

        if target_node == NODE_ID: 
            try:
                success, cid_or_logs, img_tag = await deploy_docker_container(project_id, user_id, state.get("root", "."), state.get("type", "python"), state.get("entry", "main.py"), state.get("env_vars", {}))
                if success: await asyncio.to_thread(projects_col.update_one, {"_id": project_id}, {"$set": {"status": "RUNNING", "container_id": cid_or_logs, "image_tag": img_tag, "started_at": time.time()}})
                else: await asyncio.to_thread(projects_col.update_one, {"_id": project_id}, {"$set": {"status": "CRASHED", "latest_error": cid_or_logs, "image_tag": img_tag}})
                try: fs.delete(file_id)
                except: pass
            except Exception as e: 
                await asyncio.to_thread(projects_col.update_one, {"_id": project_id}, {"$set": {"status": "ERROR", "latest_error": str(e)}})
        else: 
            await trigger_github_worker(target_node)

        anim_task.cancel()
        USER_STATE.pop(user_id, None); cleanup_workspace(user_id)
        await render_dashboard(client, prog_msg, user_id, is_edit=True)

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
            except Exception as e: 
                await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"status": "CRASHED", "latest_error": str(e)}})
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
                new_c = await asyncio.to_thread(docker_client.containers.run, image_tag, name=f"anysnap_bot_{user_id}_{int(time.time())}", detach=True, mem_limit="512m", memswap_limit="512m", nano_cpus=1_000_000_000, pids_limit=128, cap_drop=["ALL"], security_opt=["no-new-privileges:true"], read_only=True, privileged=False, network_mode="bridge", tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m", "/home/botuser/.cache": "rw,nosuid,nodev,size=64m", "/app/data": "rw,nosuid,nodev,size=128m"}, environment=proj.get("env_vars", {}), restart_policy={"Name": "on-failure", "MaximumRetryCount": 3})
                await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"container_id": new_c.id, "status": "RUNNING", "started_at": time.time()}})
            except Exception as e: 
                await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"status": "CRASHED", "latest_error": str(e)}})
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
        text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ⚙️ PROJECT SETTINGS  ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"🔐 **ENVIRONMENT**\n━━━━━━━━━━━━━━━━━━━━━━\n{env_text}\n\n"
                f"💾 **RESOURCES**\n├─ RAM       `512 MB`\n├─ CPU       `1 Core`\n└─ Processes `128`\n\n"
                f"🛡 **SECURITY**\n├─ Sandbox       🟢 ON\n├─ Privileges    🔒 Restricted\n└─ Auto Restart  🟢 ON\n━━━━━━━━━━━━━━━━━━━━━━\n"
                f"*(💡 Save temp data inside `/app/data/`)*")
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
            return await query.message.edit_text(text, reply_markup=kb)

        cid = proj.get("container_id")
        if not cid: return await query.message.edit_text("⚠️ Container ID is missing.", reply_markup=kb)

        if proj.get("target_node", 1) == NODE_ID:
            try: 
                c = await asyncio.to_thread(docker_client.containers.get, cid)
                logs = (await asyncio.to_thread(c.logs, tail=50)).decode("utf-8", errors="ignore")
            except: logs = "Log retrieval error."
            await query.message.edit_text(f"📜 **LOGS (Node 1):**\n```\n{logs[-2000:]}\n```", reply_markup=kb)
        else:
            await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"action": "get_logs"}})
            await query.message.edit_text("⏳ Fetching logs from remote node (Fast Polling)...", reply_markup=InlineKeyboardMarkup([]))
            
            # Fast Non-blocking Polling (350ms interval ~ 7 seconds max wait)
            for _ in range(20): 
                await asyncio.sleep(0.35)
                p = await asyncio.to_thread(projects_col.find_one, {"_id": proj["_id"]})
                if "action" not in p and "latest_logs" in p:
                    return await query.message.edit_text(f"📜 **LOGS (Node {proj.get('target_node', 1)}):**\n```\n{p.get('latest_logs', 'Empty')} \n```", reply_markup=kb)
            await query.message.edit_text("⏳ Remote node is slow or unresponsive. Please refresh.", reply_markup=kb)

    elif data == "btn_start":
        proj = await asyncio.to_thread(projects_col.find_one, {"user_id": user_id})
        if not proj: return await query.answer("No active bot!", show_alert=True)
        image_tag = proj.get("image_tag")
        if not image_tag: return await query.answer("Image missing. Delete & Re-deploy.", show_alert=True)

        await query.answer("🚀 Starting Bot...", show_alert=False)
        anim_task = asyncio.create_task(animate_status(query.message, proj["_id"], "start"))

        if proj.get("target_node", 1) == NODE_ID:
            try:
                new_c = await asyncio.to_thread(docker_client.containers.run, image_tag, name=f"anysnap_bot_{user_id}_{int(time.time())}", detach=True, mem_limit="512m", memswap_limit="512m", nano_cpus=1_000_000_000, pids_limit=128, cap_drop=["ALL"], security_opt=["no-new-privileges:true"], read_only=True, privileged=False, network_mode="bridge", tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m", "/home/botuser/.cache": "rw,nosuid,nodev,size=64m", "/app/data": "rw,nosuid,nodev,size=128m"}, environment=proj.get("env_vars", {}), restart_policy={"Name": "on-failure", "MaximumRetryCount": 3})
                await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"container_id": new_c.id, "status": "RUNNING", "started_at": time.time()}})
            except Exception as e: 
                await asyncio.to_thread(projects_col.update_one, {"_id": proj["_id"]}, {"$set": {"status": "CRASHED", "latest_error": str(e)}})
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

        if proj.get("status") in ["DELETING", "BUILDING", "QUEUED"] or proj.get("target_node", 1) == NODE_ID:
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
async def main_node():
    print("⏳ Starting Background Tasks...")
    asyncio.create_task(heartbeat_loop())
    
    print("👑 MAGMA CLOUD MASTER NODE STARTED: Listening for Telegram messages...")
    await app.start()
    await idle()
    await app.stop()

if __name__ == "__main__":
    if NODE_ID == 1:
        asyncio.run(main_node())
    else:
        print(f"👷 MAGMA CLOUD WORKER NODE #{NODE_ID} STARTED: Background Processing...")
        
        async def run_worker():
            asyncio.create_task(heartbeat_loop())
            await worker_node_loop()
            
        asyncio.run(run_worker())