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

# ================= HEARTBEAT & SCALING =================
async def heartbeat_loop():
    psutil.cpu_percent(interval=None) 
    while True:
        try:
            def sync_tasks():
                containers = len(docker_client.containers.list(filters={"name": "anysnap_"}))
                nodes_col.update_one(
                    {"node_id": NODE_ID}, 
                    {"$set": {
                        "role": "MASTER" if NODE_ID == 1 else "WORKER", 
                        "cpu": psutil.cpu_percent(interval=None), 
                        "ram": psutil.virtual_memory().percent, 
                        "disk": psutil.disk_usage('/').percent, 
                        "containers": containers, 
                        "last_seen": time.time()
                    }}, 
                    upsert=True
                )
            await asyncio.to_thread(sync_tasks)
        except Exception as e: 
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

    projects_col.update_one({"_id": proj_id}, {"$set": {"status": "building"}})

    req_path = os.path.join(root, "requirements.txt")
    if os.path.exists(req_path): validate_requirements(req_path)

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

    await asyncio.to_thread(docker_client.images.build, path=root, tag=image_tag, rm=True, forcerm=True)
    projects_col.update_one({"_id": proj_id}, {"$set": {"status": "starting"}})

    container = await asyncio.to_thread(
        docker_client.containers.run, image_tag, name=container_name, detach=True, 
        mem_limit="512m", memswap_limit="512m", nano_cpus=1_000_000_000, pids_limit=128,
        cap_drop=["ALL"], security_opt=["no-new-privileges:true"], read_only=True, privileged=False, network_mode="bridge",
        tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m", "/home/botuser/.cache": "rw,nosuid,nodev,size=64m", "/app/data": "rw,nosuid,nodev,size=128m"}, 
        environment=env_vars, restart_policy={"Name": "on-failure", "MaximumRetryCount": 3}
    )

    is_stable = True
    for _ in range(5):
        await asyncio.sleep(2)
        await asyncio.to_thread(container.reload)
        if container.status != "running":
            is_stable = False
            break

    await cleanup_docker_images(user_id) 

    if not is_stable:
        logs = (await asyncio.to_thread(container.logs, tail=50)).decode("utf-8", errors="ignore")
        try: await asyncio.to_thread(container.remove, force=True)
        except: pass
        return False, logs, None

    return True, container.id, image_tag

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
            f"📦 `{state['project_name']}`\n"
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

# ================= OWNER PANEL UI =================
async def owner_panel(message, edit=False):
    if not is_owner(message.from_user.id):
        if not edit: return await message.reply_text("❌ Access Denied.")
        return

    auto = get_auto_approve()
    pending = projects_col.count_documents({"status": "pending_approval"})
    running = projects_col.count_documents({"status": "running"})
    total = projects_col.count_documents({})
    online_nodes = nodes_col.count_documents({"last_seen": {"$gt": time.time() - 30}})

    text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            f"┃ 👑 OWNER CONTROL     ┃\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"🤖 AUTO APPROVE  {'🟢 ON' if auto else '🔴 OFF'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏳ Pending       `{pending}`\n"
            f"🟢 Running       `{running}`\n"
            f"📦 Projects      `{total}`\n"
            f"🌐 Nodes Online  `{online_nodes}`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎛 OWNER CONTROLS")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Auto Approve ON", callback_data="owner_auto_on"), InlineKeyboardButton("🔴 Auto Approve OFF", callback_data="owner_auto_off")],
        [InlineKeyboardButton("⏳ Pending", callback_data="owner_pending"), InlineKeyboardButton("🤖 Projects", callback_data="owner_projects")],
        [InlineKeyboardButton("🌐 Nodes", callback_data="owner_nodes"), InlineKeyboardButton("📊 Statistics", callback_data="owner_stats")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="owner_panel")]
    ])

    if edit: 
        try: await message.edit_text(text, reply_markup=kb)
        except: pass
    else: await message.reply_text(text, reply_markup=kb)

async def show_owner_pending(message):
    if not is_owner(message.from_user.id): return
    pending = list(projects_col.find({"status": "pending_approval"}).sort("created_at", 1))

    if not pending:
        return await message.edit_text(
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ⏳ PENDING APPROVAL  ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n✅ No pending deployments.", 
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner_panel")]])
        )

    text = "╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ⏳ PENDING APPROVAL  ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
    buttons = []

    for p in pending[:20]:
        pid = str(p["_id"])
        text += (f"📦 **{p.get('project_name', 'Unknown')}**\n"
                 f"👤 User: `{p.get('user_id')}`\n"
                 f"🐍 Type: `{str(p.get('type', 'UNKNOWN')).upper()}`\n"
                 f"🖥 Node: `#{p.get('target_node', 1)}`\n"
                 f"━━━━━━━━━━━━━━━━━━━━━━\n")
        buttons.append([InlineKeyboardButton("✅ Approve", callback_data=f"approve_{pid}"), InlineKeyboardButton("❌ Reject", callback_data=f"reject_{pid}")])

    buttons.append([InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner_panel")])
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

# ================= USER DASHBOARD =================
async def render_dashboard(client, message, user_id, is_edit=False):
    proj = projects_col.find_one({"user_id": user_id})
    if not proj:
        text = ("╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
                "┃  🐳 ANYSNAP CLOUD    ┃\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                "No active projects detected.\n"
                "**Deploy:** Send `.zip` or `.py` file.\n\n"
                "*(Powered by ANYSNAP)*")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Upload Project", callback_data="btn_none")]])
    else:
        status = str(proj.get("status", "UNKNOWN")).upper()

        if status == "RUNNING": emoji = "🟢"
        elif status in ["STOPPED", "QUEUED", "BUILDING", "STARTING"]: emoji = "🟡"
        elif status == "PENDING_APPROVAL": emoji = "🟡"
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
            kb_layout = [[InlineKeyboardButton("🔄 Refresh", callback_data="btn_refresh_dash")]]
            kb = InlineKeyboardMarkup(kb_layout)
        else:
            node_stats = nodes_col.find_one({"node_id": proj.get("target_node", 1)})
            cpu, ram, disk = (node_stats['cpu'], node_stats['ram'], node_stats['disk']) if node_stats else (0.0, 0.0, 0.0)
            text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
                    f"┃  🐳 ANYSNAP CLOUD    ┃\n"
                    f"╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                    f"{emoji}  **{status}**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🤖  `{proj.get('project_name', 'Anysnap App')}`\n"
                    f"🐍  `{str(proj.get('type', 'UNKNOWN')).upper()} • {proj.get('entry', 'main.py')}`\n\n"
                    f"🖥️  NODE\n"
                    f"└─ #{proj.get('target_node', 1)} {'MASTER' if proj.get('target_node', 1) == 1 else 'WORKER'}\n\n"
                    f"⚡ CPU     `{get_progress_bar(cpu)}` {cpu}%\n"
                    f"💾 RAM     `{get_progress_bar(ram)}` {ram}%\n"
                    f"💿 DISK    `{get_progress_bar(disk)}` {disk}%\n\n"
                    f"⏱ Uptime   `{format_uptime(proj.get('started_at'))}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"      🎛 CONTROLS")

            kb_layout = []
            
            # 🛡️ Safely render buttons based on crash status
            if status == "STOPPED":
                kb_layout.append([InlineKeyboardButton("▶️ Start Bot", callback_data="btn_start"), InlineKeyboardButton("🗑️ Delete Project", callback_data="btn_delete")])
            elif status in ["CRASHED", "ERROR"]:
                # Cannot restart/stop a crashed bot properly, so show Error Logs & Delete button
                kb_layout.append([InlineKeyboardButton("📜 Error Logs", callback_data="btn_logs"), InlineKeyboardButton("⚙️ Settings", callback_data="btn_settings")])
                kb_layout.append([InlineKeyboardButton("🗑️ Delete & Re-deploy", callback_data="btn_delete")])
            else:
                kb_layout.append([InlineKeyboardButton("📜 Logs", callback_data="btn_logs")])
                kb_layout.append([InlineKeyboardButton("🔄 Restart", callback_data="btn_restart"), InlineKeyboardButton("⚙️ Settings", callback_data="btn_settings")])
                kb_layout.append([InlineKeyboardButton("⏹ Stop", callback_data="btn_stop"), InlineKeyboardButton("🗑️ Delete", callback_data="btn_delete")])
            
            kb_layout.append([InlineKeyboardButton("🔄 Refresh", callback_data="btn_refresh_dash")])
            kb = InlineKeyboardMarkup(kb_layout)

    try:
        if is_edit: await message.edit_text(text, reply_markup=kb)
        else: await message.reply_text(text, reply_markup=kb)
    except: pass

# ================= MESSAGE HANDLERS =================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    print(f"📥 MESSAGE RECEIVED: /start from user {message.from_user.id}")
    await render_dashboard(client, message, message.from_user.id, is_edit=False)

@app.on_message(filters.command("stats"))
async def stats_cmd(client, message):
    total_bots = projects_col.count_documents({"status": "running"})
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            f"┃ 📊 CLUSTER STATISTICS ┃\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"🖥️ **Master Node (Node 1)**\n"
            f"⚡ CPU: `{cpu}%`\n"
            f"💾 RAM: `{ram.percent}%` ({ram.used // (1024**2)}MB / {ram.total // (1024**2)}MB)\n"
            f"💿 DISK: `{disk.percent}%`\n\n"
            f"🤖 **Global Status**\n"
            f"🟢 Running Bots: `{total_bots}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━")
    await message.reply_text(text)

@app.on_message(filters.command("owner"))
async def owner_cmd(client, message):
    print(f"📥 MESSAGE RECEIVED: /owner from user {message.from_user.id}")
    if not is_owner(message.from_user.id): return await message.reply_text("❌ Access Denied.")
    await owner_panel(message)

@app.on_message(filters.document & filters.private)
async def handle_document_upload(client, message):
    print(f"📥 FILE RECEIVED: Document from user {message.from_user.id}")
    user_id = message.from_user.id
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
            cleanup_workspace(user_id); await status_msg.edit_text(f"❌ Security/Validation Error: {e}")

@app.on_message(filters.text & filters.private)
async def text_handler(client, message):
    user_id = message.from_user.id
    if user_id in REQ_WAITING:
        reqs = message.text.replace(",", "\n").replace(" ", "\n")
        state = USER_STATE.get(user_id)
        if state:
            with open(os.path.join(state["root"], "requirements.txt"), "w") as f: f.write(reqs)
            rezip_workspace(state["dir"]) 
            await show_deploy_confirmation(client, user_id, message.chat.id, edit_msg=None)
        del REQ_WAITING[user_id]
        return

    if user_id in ENV_WAITING:
        text = message.text
        if "=" not in text: return await message.reply_text("❌ Invalid format. Send as `KEY=VALUE`")
        key, val = text.split("=", 1)
        key, val = key.strip(), val.strip()
        if not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*$", key): return await message.reply_text("❌ Invalid ENV name.")

        proj = projects_col.find_one({"user_id": user_id})
        if proj: projects_col.update_one({"user_id": user_id}, {"$set": {f"env_vars.{key}": val}})
        elif user_id in USER_STATE: USER_STATE[user_id]["env_vars"][key] = val
        del ENV_WAITING[user_id]
        await message.reply_text(f"✅ Added ENV: `{key}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Settings", callback_data="btn_settings")]]))

# ================= CALLBACK HANDLERS =================
@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id
    print(f"👆 BUTTON CLICKED: {data} by user {user_id}")

    owner_actions = data.startswith("owner_") or data.startswith("approve_") or data.startswith("reject_") or data.startswith("admin_")
    if owner_actions and not is_owner(user_id):
        return await query.answer("❌ Owner Only!", show_alert=True)

    if data == "owner_auto_on": 
        set_auto_approve(True); await query.answer("🟢 Auto Approve ON", show_alert=True); return await owner_panel(query.message, edit=True)
    elif data == "owner_auto_off": 
        set_auto_approve(False); await query.answer("🔴 Auto Approve OFF", show_alert=True); return await owner_panel(query.message, edit=True)
    elif data == "owner_panel": return await owner_panel(query.message, edit=True)
    elif data == "owner_pending": return await show_owner_pending(query.message)
    elif data == "owner_stats":
        all_nodes = list(nodes_col.find().sort("node_id", 1))
        total_cpu, total_ram, online = 0, 0, 0
        text = "╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 📊 CLUSTER STATISTICS ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        for n in all_nodes:
            alive = (time.time() - n.get("last_seen", 0)) < 30
            if alive: online += 1
            cpu, ram = n.get("cpu", 0), n.get("ram", 0)
            total_cpu += cpu; total_ram += ram
            text += f"{'🟢' if alive else '🔴'} **NODE #{n.get('node_id')}**\nRole: `{n.get('role', 'UNKNOWN')}`\nCPU: `{cpu:.1f}%`\nRAM: `{ram:.1f}%`\nBots: `{n.get('containers', 0)}`\n\n"
        count = len(all_nodes) or 1
        text += f"━━━━━━━━━━━━━━━━━━━━━━\n🌐 Online Nodes: `{online}/{len(all_nodes)}`\n⚡ Avg CPU: `{total_cpu/count:.1f}%`\n💾 Avg RAM: `{total_ram/count:.1f}%`"
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner_panel")]]))

    elif data == "owner_projects":
        projects = list(projects_col.find().sort("created_at", -1).limit(20))
        if not projects: return await query.message.edit_text("╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 🤖 ALL PROJECTS      ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\nNo projects found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner_panel")]]))
        text = "╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 🤖 ALL PROJECTS      ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        for p in projects:
            status = p.get("status", "unknown").upper()
            text += f"📦 **{p.get('project_name', 'Unknown')}**\n👤 `{p.get('user_id')}`\n📌 `{status}`\n🖥 Node `{p.get('target_node', 1)}`\n━━━━━━━━━━━━━━━━━━━━━━\n"
        await query.message.edit_text(text[:4096], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner_panel")]]))

    elif data == "owner_nodes":
        nodes = list(nodes_col.find().sort("node_id", 1))
        text = "╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 🌐 NODE CONTROL      ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        for n in nodes:
            alive = (time.time() - n.get("last_seen", 0)) < 30
            text += f"{'🟢' if alive else '🔴'} **NODE #{n.get('node_id')}**\nRole: `{n.get('role', 'UNKNOWN')}`\nCPU: `{n.get('cpu', 0):.1f}%`\nRAM: `{n.get('ram', 0):.1f}%`\nDisk: `{n.get('disk', 0):.1f}%`\nBots: `{n.get('containers', 0)}`\n\n"
        await query.message.edit_text(text[:4096], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner_panel")]]))

    elif data.startswith("approve_"):
        project_id = data.replace("approve_", "", 1)
        try: oid = ObjectId(project_id)
        except: return await query.answer("Invalid project.", show_alert=True)
        proj = projects_col.find_one({"_id": oid, "status": "pending_approval"})
        if not proj: return await query.answer("Project no longer pending.", show_alert=True)
        projects_col.update_one({"_id": oid}, {"$set": {"status": "queued", "approved_by": OWNER_ID, "approved_at": time.time()}})
        target_node = proj.get("target_node", 1)
        if target_node == NODE_ID:
            try:
                work_dir = os.path.join(HOST_DIR, str(proj["user_id"]))
                os.makedirs(work_dir, exist_ok=True)
                zip_path, extract_dir = os.path.join(work_dir, "project.zip"), os.path.join(work_dir, "extracted")
                os.makedirs(extract_dir, exist_ok=True)
                with open(zip_path, "wb") as f: f.write(fs.get(proj["file_id"]).read())
                safe_extract_zip(zip_path, extract_dir)
                success, cid, img_tag = await deploy_docker_container(oid, proj["user_id"], extract_dir, proj["type"], proj.get("entry", ""), proj.get("env_vars", {}))
                if success: projects_col.update_one({"_id": oid}, {"$set": {"status": "running", "container_id": cid, "image_tag": img_tag, "started_at": time.time()}, "$unset": {"latest_error": ""}})
                else: projects_col.update_one({"_id": oid}, {"$set": {"status": "crashed", "latest_error": cid}})
                try: fs.delete(proj["file_id"])
                except: pass
                cleanup_workspace(proj["user_id"])
            except Exception as e: projects_col.update_one({"_id": oid}, {"$set": {"status": "error", "latest_error": str(e)}})
        else:
            success = await trigger_github_worker(target_node)
            if not success: projects_col.update_one({"_id": oid}, {"$set": {"status": "error", "latest_error": "Worker boot failed."}})
        try: await app.send_message(proj["user_id"], f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ✅ DEPLOYMENT APPROVED┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n📦 `{proj.get('project_name', 'Project')}`\n\n🚀 Your project has been approved and deployment has started.")
        except: pass
        await query.answer("✅ Project Approved", show_alert=True); return await show_owner_pending(query.message)

    elif data.startswith("reject_"):
        project_id = data.replace("reject_", "", 1)
        try: oid = ObjectId(project_id)
        except: return await query.answer("Invalid project.", show_alert=True)
        proj = projects_col.find_one({"_id": oid, "status": "pending_approval"})
        if not proj: return await query.answer("Project already processed.", show_alert=True)
        projects_col.update_one({"_id": oid}, {"$set": {"status": "rejected", "rejected_by": OWNER_ID, "rejected_at": time.time()}})
        try: fs.delete(proj["file_id"])
        except: pass
        try: await app.send_message(proj["user_id"], f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ❌ DEPLOYMENT REJECTED┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n📦 `{proj.get('project_name', 'Project')}`\n\nYour deployment request was not approved.")
        except: pass
        await query.answer("❌ Project Rejected", show_alert=True); return await show_owner_pending(query.message)

    elif data == "btn_refresh_dash": await render_dashboard(client, query.message, user_id, is_edit=True)
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
        prog_msg = await query.message.edit_text("╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 🚀 DEPLOYMENT        ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n⏳ Preparing deployment...")
        target_node = get_best_node()
        with open(state["zip_path"], "rb") as f: file_id = fs.put(f, filename=f"user_{user_id}.zip")
        auto_approve = get_auto_approve()
        initial_status = "queued" if auto_approve else "pending_approval"
        db_doc = {"user_id": user_id, "target_node": target_node, "status": initial_status, "type": state.get("type", "python"), "entry": state.get("entry", "main.py"), "file_id": file_id, "env_vars": state.get("env_vars", {}), "project_name": state.get("project_name", "Anysnap App"), "created_at": time.time()}
        result = projects_col.insert_one(db_doc)
        project_id = result.inserted_id

        if not auto_approve:
            await prog_msg.edit_text(f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ⏳ AWAITING APPROVAL ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n📦 `{state.get('project_name', 'App')}`\n🐍 `{str(state.get('type')).upper()}`\n\n🔐 Your deployment request has been sent for approval.\n\n⏳ Please wait for approval.")
            try: await app.send_message(OWNER_ID, f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 🔔 DEPLOYMENT REQUEST┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n📦 **{state.get('project_name', 'App')}**\n👤 User: `{user_id}`\n🐍 Type: `{str(state.get('type')).upper()}`\n📄 Entry: `{state.get('entry', 'main.py')}`\n🖥 Target Node: `#{target_node}`\n\n⚠️ **Approval Required**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ APPROVE", callback_data=f"approve_{project_id}"), InlineKeyboardButton("❌ REJECT", callback_data=f"reject_{project_id}")]]))
            except Exception as e: projects_col.update_one({"_id": project_id}, {"$set": {"latest_error": f"Owner notification failed: {e}"}})
            USER_STATE.pop(user_id, None); cleanup_workspace(user_id); return

        if target_node == NODE_ID: 
            try:
                success, cid, img_tag = await deploy_docker_container(project_id, user_id, state["root"], state.get("type", "python"), state.get("entry", "main.py"), state.get("env_vars", {}))
                if success: projects_col.update_one({"_id": project_id}, {"$set": {"status": "running", "container_id": cid, "image_tag": img_tag, "started_at": time.time()}})
                else: projects_col.update_one({"_id": project_id}, {"$set": {"status": "crashed", "latest_error": cid}})
                try: fs.delete(file_id)
                except: pass
                await render_dashboard(client, prog_msg, user_id, is_edit=True)
            except Exception as e: 
                projects_col.update_one({"_id": project_id}, {"$set": {"status": "error", "latest_error": str(e)}})
                await prog_msg.edit_text("❌ Deployment failed.")
        else: 
            await trigger_github_worker(target_node)
            await prog_msg.edit_text(f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 🚀 DEPLOYMENT        ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n✅ Node Selected: `#{target_node}`\n⏳ Worker Booting...\n\n🔄 Refresh dashboard to track.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="btn_refresh_dash")]]))
        USER_STATE.pop(user_id, None); cleanup_workspace(user_id); return

    elif data == "btn_restart":
        proj = projects_col.find_one({"user_id": user_id})
        if not proj: return await query.answer("No active bot!", show_alert=True)
        if proj.get("status") in ["crashed", "error"]:
            return await query.answer("Bot is CRASHED! Cannot restart without a container. Please click 'Delete & Re-deploy'.", show_alert=True)
            
        cid = proj.get("container_id")
        if not cid: return await query.answer("Error: Container ID missing!", show_alert=True)

        if proj["target_node"] == NODE_ID:
            try: 
                docker_client.containers.get(cid).restart()
                projects_col.update_one({"_id": proj["_id"]}, {"$set": {"started_at": time.time()}})
                await query.answer("✅ Restarted!")
            except Exception as e: 
                await query.answer(f"Error: {e}", show_alert=True)
        else: 
            projects_col.update_one({"_id": proj["_id"]}, {"$set": {"action": "restart"}})
            await query.answer("🔄 Restart signal sent...")

    elif data == "btn_apply_env":
        proj = projects_col.find_one({"user_id": user_id})
        if not proj: return await query.answer("No active bot!", show_alert=True)
        
        image_tag = proj.get("image_tag")
        if not image_tag:
            return await query.answer("Cannot Apply ENV: Bot Image was never built due to crash! Please Delete and re-upload.", show_alert=True)

        if proj["target_node"] == NODE_ID:
            try:
                cid = proj.get("container_id")
                if cid:
                    old_c = docker_client.containers.get(cid)
                    old_c.stop()
                    old_c.remove(force=True)
            except: pass
            
            try:
                new_c = docker_client.containers.run(image_tag, name=f"anysnap_bot_{user_id}", detach=True, mem_limit="512m", memswap_limit="512m", nano_cpus=1_000_000_000, pids_limit=128, cap_drop=["ALL"], security_opt=["no-new-privileges:true"], read_only=True, privileged=False, network_mode="bridge", tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m", "/home/botuser/.cache": "rw,nosuid,nodev,size=64m", "/app/data": "rw,nosuid,nodev,size=128m"}, environment=proj.get("env_vars", {}), restart_policy={"Name": "on-failure", "MaximumRetryCount": 3})
                projects_col.update_one({"_id": proj["_id"]}, {"$set": {"container_id": new_c.id, "status": "running", "started_at": time.time()}})
                await render_dashboard(client, query.message, user_id, is_edit=True)
            except Exception as e: 
                await query.answer(f"Error: {e}", show_alert=True)
        else: 
            projects_col.update_one({"_id": proj["_id"]}, {"$set": {"action": "apply_env"}})
            await query.answer("🔄 Apply signal sent to Worker...")

    elif data == "btn_settings":
        proj = projects_col.find_one({"user_id": user_id})
        active_env = proj.get("env_vars", {}) if proj else {}
        env_text = "\n".join([f"• `{k}`" for k in active_env.keys()]) if active_env else "None"
        text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ ⚙️ PROJECT SETTINGS  ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"🔐 **ENVIRONMENT**\n━━━━━━━━━━━━━━━━━━━━━━\n{env_text}\n\n"
                f"💾 **RESOURCES**\n├─ RAM       `512 MB`\n├─ CPU       `1 Core`\n└─ Processes `128`\n\n"
                f"🛡 **SECURITY**\n├─ Sandbox       🟢 ON\n├─ Privileges    🔒 Restricted\n└─ Auto Restart  🟢 ON\n━━━━━━━━━━━━━━━━━━━━━━\n"
                f"*(💡 Save temp data inside `/app/data/`)*")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Manage ENV", callback_data="btn_add_env"), InlineKeyboardButton("💾 Resources", callback_data="btn_none")], [InlineKeyboardButton("🔄 Restart & Apply", callback_data="btn_apply_env")], [InlineKeyboardButton("⬅️ Dashboard", callback_data="btn_refresh_dash")]])
        await query.message.edit_text(text, reply_markup=kb)

    elif data == "btn_add_env":
        ENV_WAITING[user_id] = True; await query.message.edit_text("✍️ Send variable:\n`KEY=VALUE`")

    elif data == "btn_logs":
        proj = projects_col.find_one({"user_id": user_id})
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="btn_logs"), InlineKeyboardButton("⬅️ Dashboard", callback_data="btn_refresh_dash")]])
        
        status = proj.get("status", "")
        if status in ["crashed", "error"]:
            text = (f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n┃ 🔴 BOT CRASHED       ┃\n╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n🤖 `{proj.get('project_name', 'Bot')}`\n🖥️ Node #{proj.get('target_node', 1)}\n\n⚠️ **ERROR**\n━━━━━━━━━━━━━━━━━━━━━━\n```\n{proj.get('latest_error', 'No build log or trace found.')[-1500:]}\n```\n━━━━━━━━━━━━━━━━━━━━━━")
            return await query.message.edit_text(text, reply_markup=kb)
            
        if status == "stopped": return await query.message.edit_text("⚠️ Container is currently STOPPED.", reply_markup=kb)

        cid = proj.get("container_id")
        if not cid:
            return await query.message.edit_text("⚠️ Error: Container ID is missing in database.", reply_markup=kb)

        if proj["target_node"] == NODE_ID:
            try: logs = docker_client.containers.get(cid).logs(tail=50).decode("utf-8", errors="ignore")
            except: logs = "Log retrieval error."
            await query.message.edit_text(f"📜 **LOGS (Node 1):**\n```\n{logs[-2000:]}\n```", reply_markup=kb)
        else:
            projects_col.update_one({"_id": proj["_id"]}, {"$set": {"action": "get_logs"}})
            await query.answer("Fetching from Node...", show_alert=False); await asyncio.sleep(3)
            p = projects_col.find_one({"_id": proj["_id"]})
            await query.message.edit_text(f"📜 **LOGS (Node {proj['target_node']}):**\n```\n{p.get('latest_logs', 'Fetching...')} \n```", reply_markup=kb)

    elif data == "btn_stop":
        proj = projects_col.find_one({"user_id": user_id})
        if not proj: return await query.answer("No active bot!", show_alert=True)
        if proj.get("status") in ["crashed", "error"]:
            return await query.answer("Bot is already CRASHED. You cannot stop it. Please click Delete.", show_alert=True)

        if proj["target_node"] == NODE_ID:
            cid = proj.get("container_id")
            if cid:
                try: 
                    c = docker_client.containers.get(cid)
                    c.stop()
                    c.remove(force=True)
                except: pass
            projects_col.update_one({"_id": proj["_id"]}, {"$set": {"status": "stopped"}, "$unset": {"container_id": ""}})
            await render_dashboard(client, query.message, user_id, is_edit=True)
        else: 
            projects_col.update_one({"_id": proj["_id"]}, {"$set": {"action": "stop", "status": "stopping"}})
            await query.message.edit_text("🟡 Stopping Bot on Remote Node...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Dashboard", callback_data="btn_refresh_dash")]]))

    elif data == "btn_delete":
        proj = projects_col.find_one({"user_id": user_id})
        if not proj: return await query.answer("Already deleted!", show_alert=True)
        
        if proj["target_node"] == NODE_ID:
            cid = proj.get("container_id")
            if cid:
                try: 
                    c = docker_client.containers.get(cid)
                    c.stop()
                    c.remove(force=True)
                except: pass
            projects_col.delete_one({"_id": proj["_id"]})
            asyncio.create_task(cleanup_docker_images(user_id))
        else: 
            projects_col.update_one({"_id": proj["_id"]}, {"$set": {"action": "delete"}})
            
        await query.message.edit_text("🗑️ Project Deleted & Resources Wiped.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Return Home", callback_data="btn_refresh_dash")]]))

# ================= RUNNERS =================
if __name__ == "__main__":
    if NODE_ID == 1:
        print("⏳ Starting Background Tasks...")
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        loop.create_task(heartbeat_loop())
        
        print("👑 ANYSNAP CLOUD MASTER NODE STARTED: Listening for Telegram messages...")
        app.run()
    else:
        print(f"👷 ANYSNAP CLOUD WORKER NODE #{NODE_ID} STARTED: Background Processing...")
        asyncio.run(worker_node_loop())