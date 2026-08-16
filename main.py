import os
import shutil
import zipfile
import time
import json
import asyncio
import docker
import psutil
import requests
from pymongo import MongoClient
import gridfs
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from pyrogram.errors import MessageNotModified

import config

# ================= CONFIG & GLOBALS =================
HOST_DIR = "hosted_containers"
MAX_ZIP_SIZE = 50 * 1024 * 1024       
MAX_EXTRACTED_SIZE = 200 * 1024 * 1024 
MAX_FILES = 500
MAX_ACTIONS = 7

os.makedirs(HOST_DIR, exist_ok=True)

# Node ID auto-fetch karega GitHub env se. Default 1 (Master)
NODE_ID = int(os.getenv("NODE_ID", 1))

try:
    docker_client = docker.from_env()
except Exception as e:
    print(f"❌ Docker Error: Make sure Docker daemon is running! Details: {e}")
    exit(1)

# ================= MONGODB SETUP (GRIDFS & DB) =================
try:
    mongo_client = MongoClient(getattr(config, "MONGO_URI", os.getenv("MONGO_URI")))
    db = mongo_client["anysnap_paas"]
    projects_col = db["projects"]
    fs = gridfs.GridFS(db)
except Exception as e:
    print(f"❌ MongoDB Error: Invalid URI! Check your config. Details: {e}")
    exit(1)

app = Client("AnysnapPaaSManager", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)
USER_STATE = {}
USER_LOCKS = {}          

def get_user_lock(user_id):
    if user_id not in USER_LOCKS:
        USER_LOCKS[user_id] = asyncio.Lock()
    return USER_LOCKS[user_id]

# ================= SCALER & CORE FUNCTIONS =================
def check_system_full():
    """Check if Node 1 RAM is below 3GB or CPU > 90%"""
    free_ram_gb = psutil.virtual_memory().available / (1024 ** 3)
    cpu = psutil.cpu_percent(interval=1)
    print(f"📊 [System Check] RAM: {free_ram_gb:.2f}GB Free | CPU: {cpu}%")
    return free_ram_gb <= 3.0 or cpu >= 90.0

def trigger_github_worker(target_node):
    """Triggers the next Github Action workflow"""
    token = getattr(config, "GITHUB_TOKEN", os.getenv("GITHUB_TOKEN"))
    repo = getattr(config, "GITHUB_REPO", os.getenv("GITHUB_REPO"))
    workflow = getattr(config, "WORKFLOW_FILE", os.getenv("WORKFLOW_FILE", "main.yml"))
    
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    payload = {"ref": "main", "inputs": {"node_id": str(target_node)}}
    
    res = requests.post(url, headers=headers, json=payload)
    return res.status_code == 204

async def deploy_docker_container(user_id, root, project_type, cmd, is_auto_dockerfile=True):
    """Universal Docker build block for both Master and Worker nodes"""
    image_tag = f"anysnap_{user_id}_{int(time.time())}"
    dockerfile_path = os.path.join(root, "Dockerfile")
    
    if is_auto_dockerfile:
        try: os.remove(dockerfile_path)
        except: pass

    if not os.path.exists(dockerfile_path):
        base_img = "python:3.12-slim" if project_type == "python" else "node:22-alpine"
        install_step = ""
        # 🔥 TUMHARA AUTO-DOTENV AUR NESTED INSTALL SYSTEM
        if project_type == "python": 
            install_step = (
                "RUN apt-get update && "
                "apt-get install -y --no-install-recommends gcc g++ make build-essential && "
                "rm -rf /var/lib/apt/lists/*\n"
                "RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel\n"
                "RUN python -m pip install --no-cache-dir python-dotenv\n"
                "RUN find /app -type f -iname 'requirements.txt' "
                "-exec python -m pip install --no-cache-dir -r '{}' \\;\n"
            )
        elif project_type == "node": 
            install_step = "RUN find /app -type f -iname 'package.json' -execdir npm install \\;\n"
        
        df_content = f"FROM {base_img}\nWORKDIR /app\nCOPY . /app/\n{install_step}{cmd}\n"
        with open(dockerfile_path, "w") as df: df.write(df_content)
    
    await asyncio.to_thread(docker_client.images.build, path=root, tag=image_tag, rm=True, forcerm=True)
    container = await asyncio.to_thread(docker_client.containers.run, image_tag, detach=True, mem_limit="512m")
    
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
        return False, logs
        
    await asyncio.to_thread(container.update, restart_policy={"Name": "on-failure", "MaximumRetryCount": 3})
    return True, container.id

# ================= WORKER NODE LOOP (NODE 2, 3, 4...) =================
async def worker_node_loop():
    print(f"👷 WORKER NODE #{NODE_ID} ACTIVE! Scanning MongoDB for jobs...")
    while True:
        # 1. Naya deployment task dhundo
        task = projects_col.find_one({"target_node": NODE_ID, "status": "pending"})
        if task:
            print(f"📦 Found task for User {task['user_id']}")
            projects_col.update_one({"_id": task["_id"]}, {"$set": {"status": "deploying"}})
            
            try:
                # MongoDB se ZIP download karo
                file_data = fs.get(task["file_id"]).read()
                work_dir = os.path.join(HOST_DIR, str(task["user_id"]))
                os.makedirs(work_dir, exist_ok=True)
                zip_path = os.path.join(work_dir, "project.zip")
                with open(zip_path, "wb") as f: f.write(file_data)
                
                extract_dir = os.path.join(work_dir, "extracted")
                with zipfile.ZipFile(zip_path, 'r') as z: z.extractall(extract_dir)
                
                # Deploy
                success, result = await deploy_docker_container(task["user_id"], extract_dir, task["type"], task["cmd"])
                
                if success:
                    projects_col.update_one({"_id": task["_id"]}, {"$set": {"status": "running", "container_id": result}})
                else:
                    projects_col.update_one({"_id": task["_id"]}, {"$set": {"status": "crashed", "latest_logs": result}})
            except Exception as e:
                print(f"Worker deploy error: {e}")
                projects_col.update_one({"_id": task["_id"]}, {"$set": {"status": "error"}})

        # 2. Stop/Restart/Logs ki commands dhundo
        cmd_task = projects_col.find_one({"target_node": NODE_ID, "action": {"$exists": True}})
        if cmd_task:
            action = cmd_task["action"]
            cid = cmd_task.get("container_id")
            if cid:
                try:
                    container = docker_client.containers.get(cid)
                    if action == "get_logs":
                        logs = container.logs(tail=30).decode("utf-8", errors="ignore")
                        projects_col.update_one({"_id": cmd_task["_id"]}, {"$set": {"latest_logs": logs}, "$unset": {"action": ""}})
                    elif action == "restart":
                        container.restart()
                        projects_col.update_one({"_id": cmd_task["_id"]}, {"$unset": {"action": ""}})
                    elif action == "stop":
                        container.stop()
                        container.remove(force=True)
                        projects_col.delete_one({"_id": cmd_task["_id"]})
                except:
                    projects_col.update_one({"_id": cmd_task["_id"]}, {"$unset": {"action": ""}})
                    
        await asyncio.sleep(3) # Reduce database load

# ================= MASTER NODE LOGIC (Pyrogram) =================
def detect_project_entry(bot_dir):
    for root, _, files in os.walk(bot_dir):
        files_lower = [f.lower() for f in files]
        rel_dir = os.path.relpath(root, bot_dir)
        cd_cmd = f"cd '{rel_dir}' && " if rel_dir != "." else ""
        py_files = [f for f in files if f.endswith('.py')]
        has_req = "requirements.txt" in files_lower

        if has_req or py_files:
            python_priority = ["main.py", "bot.py", "app.py", "server.py", "run.py", "sting.py", "index.py"]
            for file in python_priority:
                if file in files: return {"type": "python", "cmd": f'CMD ["sh", "-c", "{cd_cmd}python {file}"]', "root": bot_dir, "is_auto_dockerfile": True}
            if len(py_files) >= 1: return {"type": "python", "cmd": f'CMD ["sh", "-c", "{cd_cmd}python {py_files[0]}"]', "root": bot_dir, "is_auto_dockerfile": True}

        if "package.json" in files_lower:
            try:
                with open(os.path.join(root, files[files_lower.index("package.json")]), 'r') as f:
                    if "start" in json.load(f).get("scripts", {}): return {"type": "node", "cmd": f'CMD ["sh", "-c", "{cd_cmd}npm start"]', "root": bot_dir, "is_auto_dockerfile": True}
            except: pass

    for root, _, files in os.walk(bot_dir):
        files_lower = [f.lower() for f in files]
        if "dockerfile" in files_lower:
            df_name = files[files_lower.index("dockerfile")]
            if df_name != "Dockerfile": os.rename(os.path.join(root, df_name), os.path.join(root, "Dockerfile"))
            return {"type": "docker", "cmd": "", "root": root, "is_auto_dockerfile": False}
    return None

def get_main_keyboard(user_id):
    kb = [[InlineKeyboardButton("📂 Upload ZIP", callback_data="btn_upload_info")]]
    proj = projects_col.find_one({"user_id": user_id})
    if proj and proj.get("container_id"):
        kb.append([InlineKeyboardButton("🔴 STOP & WIPE", callback_data="btn_stop")])
    elif user_id in USER_STATE and "type" in USER_STATE[user_id]:
        kb.append([InlineKeyboardButton("🚀 DEPLOY TO CLOUD", callback_data="btn_deploy")])
    return InlineKeyboardMarkup(kb)

def get_logs_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Logs", callback_data="btn_logs_refresh"), InlineKeyboardButton("📜 Full Logs", callback_data="btn_logs_full")],
        [InlineKeyboardButton("🔄 Restart", callback_data="btn_restart"), InlineKeyboardButton("🔴 Stop", callback_data="btn_stop")]
    ])

@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    user_id = message.from_user.id
    if user_id not in USER_STATE: USER_STATE[user_id] = {}
    await message.reply_text("<b>🐳 ANYSNAP CLOUD PAAS</b>\n\nScale up to 7 Servers Auto-Magically! Send your `.zip` file.", reply_markup=get_main_keyboard(user_id))

@app.on_message(filters.document & filters.private)
async def handle_zip_upload(client, message):
    user_id = message.from_user.id
    lock = get_user_lock(user_id)
    if lock.locked(): return await message.reply_text("⚠️ Processing...")
        
    async with lock:
        if not message.document.file_name.endswith('.zip'): return await message.reply_text("❌ Please send `.zip`")
        status_msg = await message.reply_text("📥 Downloading & Checking...")
        
        user_dir = os.path.join(HOST_DIR, str(user_id))
        shutil.rmtree(user_dir, ignore_errors=True)
        os.makedirs(user_dir, exist_ok=True)
        zip_path = os.path.join(user_dir, "project.zip")
        extract_dir = os.path.join(user_dir, "extracted")
        
        try:
            await message.download(file_name=zip_path)
            with zipfile.ZipFile(zip_path, 'r') as z: z.extractall(extract_dir)
            
            project_data = detect_project_entry(extract_dir)
            if project_data:
                USER_STATE[user_id] = {**project_data, "action": None, "dir": user_dir, "zip_path": zip_path}
                await status_msg.edit_text(f"✅ **Detected:** `{project_data['type'].upper()}`\n🚀 **Ready!**", reply_markup=get_main_keyboard(user_id))
            else:
                await status_msg.edit_text("⚠️ Runtime Not Found. Custom setup required.")
        except Exception as e:
            await status_msg.edit_text(f"❌ **Error:** {e}")

@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id
    lock = get_user_lock(user_id)
    if user_id not in USER_STATE: USER_STATE[user_id] = {}

    if data == "btn_deploy":
        if lock.locked(): return await query.answer("Deploying...", show_alert=True)
        async with lock:
            state = USER_STATE.get(user_id)
            if not state: return await query.answer("Upload again.", show_alert=True)
            
            project_type, root, cmd = state["type"], state["root"], state["cmd"]
            
            await query.message.edit_text("📊 Checking Cloud Resources...")
            is_full = check_system_full()
            
            # Master/Worker Routing Logic
            cluster = db.cluster.find_one({"_id": "status"})
            active_nodes = cluster["active_nodes"] if cluster else 1
            
            if is_full:
                if active_nodes >= MAX_ACTIONS:
                    return await query.message.edit_text("❌ All 7 Cloud Nodes are full!")
                
                target_node = active_nodes + 1
                await query.message.edit_text(f"⚠️ Node 1 Full! Uploading to MongoDB for Node #{target_node}...")
                
                # ZIP GridFS me dalo Worker ke liye
                with open(state["zip_path"], "rb") as f:
                    file_id = fs.put(f, filename=f"user_{user_id}.zip")
                
                projects_col.update_one({"user_id": user_id}, {"$set": {"target_node": target_node, "status": "pending", "type": project_type, "cmd": cmd, "file_id": file_id}}, upsert=True)
                db.cluster.update_one({"_id": "status"}, {"$set": {"active_nodes": target_node}}, upsert=True)
                
                trigger_github_worker(target_node)
                await query.message.edit_text(f"✅ **Sent to Cloud Node #{target_node}!**\nGithub Action boot ho raha hai. Thodi der me Logs check karein.", reply_markup=get_logs_keyboard())
            
            else:
                # Master node local run
                await query.message.edit_text("🏗️ Setting up on Master Node...")
                success, result = await deploy_docker_container(user_id, root, project_type, cmd)
                if success:
                    projects_col.update_one({"user_id": user_id}, {"$set": {"target_node": 1, "status": "running", "container_id": result}}, upsert=True)
                    await query.message.edit_text(f"✅ **Bot Run Ho Gaya (Node 1)!**\n📦 ID: `{result[:10]}`", reply_markup=get_logs_keyboard())
                else:
                    await query.message.edit_text(f"❌ **Crash!**\n```\n{result[-1500:]}\n```")

    elif data.startswith("btn_logs_"):
        proj = projects_col.find_one({"user_id": user_id})
        if not proj: return await query.answer("No active bot!", show_alert=True)
        
        if proj["target_node"] == 1:
            try:
                container = docker_client.containers.get(proj["container_id"])
                logs = container.logs(tail=30).decode("utf-8", errors="ignore")
                await query.message.edit_text(f"📜 **Local Logs:**\n```\n{logs[-2000:]}\n```", reply_markup=get_logs_keyboard())
            except Exception as e: await query.answer(f"Log Error: {e}")
        else:
            await query.answer("Fetching from Worker Node...")
            projects_col.update_one({"_id": proj["_id"]}, {"$set": {"action": "get_logs"}})
            for _ in range(8):
                await asyncio.sleep(1)
                p = projects_col.find_one({"_id": proj["_id"]})
                if "action" not in p and "latest_logs" in p:
                    return await query.message.edit_text(f"📜 **Node {proj['target_node']} Logs:**\n```\n{p['latest_logs'][-2000:]}\n```", reply_markup=get_logs_keyboard())
            await query.message.edit_text("⏳ Node slow hai, refresh again.")

    elif data == "btn_stop":
        proj = projects_col.find_one({"user_id": user_id})
        if not proj: return await query.message.edit_text("🛑 Wiped Clean!", reply_markup=get_main_keyboard(user_id))
        
        if proj["target_node"] == 1:
            try:
                container = docker_client.containers.get(proj["container_id"])
                container.stop()
                container.remove(force=True)
            except: pass
            projects_col.delete_one({"_id": proj["_id"]})
        else:
            projects_col.update_one({"_id": proj["_id"]}, {"$set": {"action": "stop"}})
            
        await query.message.edit_text("🛑 Command Sent! Wiping...", reply_markup=get_main_keyboard(user_id))

if __name__ == "__main__":
    if NODE_ID == 1:
        print("👑 MASTER NODE STARTED: Running Telegram Bot...")
        app.run()
    else:
        print(f"👷 WORKER NODE #{NODE_ID} STARTED: Background Processing Only...")
        asyncio.run(worker_node_loop())