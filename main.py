import os
import shutil
import zipfile
import ast
import sys
import time
import json
import base64
import asyncio
from nacl import encoding, public
from github import Github, Auth, GithubException
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

import config

# ================= CONFIG & GLOBALS =================
ACCOUNTS_FILE = "accounts.json"
HOST_DIR = "temp_uploads"
os.makedirs(HOST_DIR, exist_ok=True)

app = Client("EnterpriseHostingManager", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)

USER_STATE = {}
ACCOUNTS_DATA = {}

# ================= ACCOUNT MANAGER =================
def load_accounts():
    global ACCOUNTS_DATA
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r") as f:
                ACCOUNTS_DATA = json.load(f)
        except Exception:
            ACCOUNTS_DATA = {}

def save_accounts():
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(ACCOUNTS_DATA, f, indent=4)

load_accounts()

# ================= HELPER FUNCTIONS =================
def cleanup_state(user_id):
    state = USER_STATE.get(user_id)
    if state and "dir" in state and os.path.exists(state["dir"]):
        try: shutil.rmtree(state["dir"])
        except: pass
    if user_id in USER_STATE:
        del USER_STATE[user_id]

def encrypt_github_secret(public_key: str, secret_value: str) -> str:
    public_key_bytes = base64.b64decode(public_key)
    sealed_box = public.SealedBox(public.PublicKey(public_key_bytes))
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")

def update_gh_secret(repo, secret_name, secret_value):
    pub_key = repo.get_public_key()
    encrypted_value = encrypt_github_secret(pub_key.key, secret_value)
    repo.create_secret(secret_name, encrypted_value, pub_key.key_id)

def parse_env_file(bot_dir, repo):
    """FEATURE 8: Parse .env template automatically"""
    env_path = os.path.join(bot_dir, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    update_gh_secret(repo, k.strip().upper(), v.strip())
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

def build_run_command(entry_file):
    if " " in entry_file: return entry_file
    if entry_file.endswith(".py"): return f"python3 {entry_file}"
    elif entry_file.endswith(".js"): return f"node {entry_file}"
    return f"bash {entry_file}"

def parse_repo_branch(repo_str):
    if ":" in repo_str: return repo_str.split(":", 1)
    return repo_str, "main"

# ================= GITHUB SMART LOAD BALANCER =================
def get_best_runner(user_id):
    """FEATURE 1: Multi-Repo Support - Finds the least busy repository"""
    repos_list = ACCOUNTS_DATA.get(str(user_id), [])
    if not repos_list: return None
    
    best_rd = None
    best_repo = None
    best_branch = None
    min_runs = 9999

    for rd in repos_list:
        try:
            gh = Github(auth=Auth.Token(rd["token"]))
            r_name, branch = parse_repo_branch(rd["repo"])
            repo = gh.get_repo(r_name)
            
            # Count in-progress runs
            runs = repo.get_workflow_runs(status="in_progress").totalCount
            if runs < min_runs:
                min_runs = runs
                best_rd = rd
                best_repo = repo
                best_branch = branch
        except Exception:
            continue # Skip failed/rate-limited repos
            
    return (best_rd, best_repo, best_branch) if best_repo else None

async def poll_deployment_status(client, user_id, repo, msg_id):
    """FEATURE 6: Polling Webhook Simulation"""
    await asyncio.sleep(15) # Wait for Github to register the dispatch
    try:
        runs = repo.get_workflow_runs(event="repository_dispatch", status="in_progress")
        if runs.totalCount > 0:
            run = runs[0]
            for _ in range(30): # Poll for 15 mins max (30 * 30s)
                run.update()
                if run.status == "completed":
                    conclusion = run.conclusion
                    emoji = "✅" if conclusion == "success" else "❌"
                    await client.send_message(user_id, f"{emoji} **Deployment Status:** {conclusion.upper()}\n🔗 [View Logs]({run.html_url})", reply_to_message_id=msg_id)
                    return
                await asyncio.sleep(30)
            await client.send_message(user_id, "⚠️ **Deployment Timeout:** Polling stopped, but bot might still be running.", reply_to_message_id=msg_id)
    except Exception as e:
        pass

async def push_folder_to_github(repo, branch, local_dir, status_msg):
    all_files = []
    for root, _, files in os.walk(local_dir):
        for file in files: all_files.append(os.path.join(root, file))
    
    total = len(all_files)
    if total == 0: return

    for i, file_path in enumerate(all_files):
        if i % 5 == 0 or i == total - 1:
            await status_msg.edit_text(f"📤 Pushing files to GitHub... ({i+1}/{total})\nFile: `{os.path.basename(file_path)}`")
        
        rel_path = os.path.relpath(file_path, local_dir).replace("\\", "/")
        with open(file_path, "rb") as f:
            content = f.read()
        try:
            file_info = repo.get_contents(rel_path, ref=branch)
            repo.update_file(rel_path, f"Update {rel_path}", content, file_info.sha, branch=branch)
        except GithubException as e:
            if e.status == 404:
                repo.create_file(rel_path, f"Create {rel_path}", content, branch=branch)

async def process_and_sync(client, user_id, status_msg):
    state = USER_STATE[user_id]
    
    best_runner = get_best_runner(user_id)
    if not best_runner:
        return await status_msg.edit_text("❌ No available GitHub runner found. Please add an account or check rate limits.")
    
    rd, repo, branch = best_runner
    bot_dir = state["dir"]
    entry_file = state["entry"]
    run_cmd = build_run_command(entry_file)
    
    await status_msg.edit_text(f"🎯 **Smart Balancer Selected:** `{repo.full_name}`\n🔄 Updating Secrets...")
    
    # Update command
    update_gh_secret(repo, "RUN_COMMAND", run_cmd)
    
    # Parse .env if exists
    if parse_env_file(bot_dir, repo):
        await status_msg.edit_text(f"🎯 **Smart Balancer Selected:** `{repo.full_name}`\n🔐 .env variables securely loaded!")
        
    await push_folder_to_github(repo, branch, bot_dir, status_msg)
    
    msg = f"✅ **Sync Complete to `{repo.full_name}`!**\n📂 Main Entry: `{entry_file}`\n⚙️ Command: `{run_cmd}`"
    
    if state.get("auto_deploy", False):
        await status_msg.edit_text(msg + "\n\n⚡ Auto-Deploy Triggered! Dispatching runner...")
        repo.create_dispatch_event("deploy_bot")
        msg += f"\n\n🚀 **Deployed!** Bot is polling for status in background..."
        asyncio.create_task(poll_deployment_status(client, user_id, repo, status_msg.id))
    
    await status_msg.edit_text(msg, reply_markup=get_main_keyboard(user_id))
    cleanup_state(user_id)

# ================= KEYBOARDS =================
def get_main_keyboard(user_id):
    auto_deploy = USER_STATE.get(user_id, {}).get("auto_deploy", False)
    ad_text = "🟢 Auto-Deploy: ON" if auto_deploy else "🔴 Auto-Deploy: OFF"
    
    repos_count = len(ACCOUNTS_DATA.get(str(user_id), []))
    
    kb = [
        [InlineKeyboardButton(f"➕ Add Runner Repo (Total: {repos_count})", callback_data="btn_add_acc")],
        [InlineKeyboardButton(ad_text, callback_data="btn_toggle_autodeploy")],
        [InlineKeyboardButton("📊 Runner Pool Status", callback_data="btn_pool")],
        [InlineKeyboardButton("🚀 DEPLOY BOT (Smart)", callback_data="btn_deploy"),
         InlineKeyboardButton("🛑 STOP ALL", callback_data="btn_stop")],
        [InlineKeyboardButton("🔧 Add Extra Env Vars", callback_data="btn_edit_env")]
    ]
    return InlineKeyboardMarkup(kb)

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Cancel", callback_data="btn_cancel")]])

# ================= COMMANDS & CALLBACKS =================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    user_id = message.from_user.id
    if user_id not in USER_STATE: USER_STATE[user_id] = {"auto_deploy": False}
    await message.reply_text("<b>👑 Enterprise Load Balancer Manager</b>\n\nUpload `.zip`, `.py`, or `.js`.\nThe load balancer will auto-select the best free GitHub runner to deploy your code.", reply_markup=get_main_keyboard(user_id))

@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id
    if user_id not in USER_STATE: USER_STATE[user_id] = {"auto_deploy": False}

    if data == "btn_cancel":
        cleanup_state(user_id)
        await query.message.edit_text("🚫 Action cancelled.", reply_markup=get_main_keyboard(user_id))

    elif data == "btn_toggle_autodeploy":
        USER_STATE[user_id]["auto_deploy"] = not USER_STATE[user_id].get("auto_deploy", False)
        await query.message.edit_reply_markup(get_main_keyboard(user_id))

    elif data == "btn_add_acc":
        USER_STATE[user_id].update({"action": "wait_token", "timestamp": time.time()})
        await query.message.edit_text("🔑 Send your **GitHub Personal Access Token**:", reply_markup=get_cancel_keyboard())

    elif data == "btn_pool":
        repos_list = ACCOUNTS_DATA.get(str(user_id), [])
        if not repos_list: return await query.answer("No repos added to pool!", show_alert=True)
        stats = "📊 **Runner Pool Status:**\n\n"
        for i, rd in enumerate(repos_list):
            try:
                gh = Github(auth=Auth.Token(rd["token"]))
                r_name, branch = parse_repo_branch(rd["repo"])
                runs = gh.get_repo(r_name).get_workflow_runs(status="in_progress").totalCount
                stats += f"{i+1}. `{r_name}` -> {runs} Active Runs\n"
            except Exception as e:
                stats += f"{i+1}. `{rd['repo']}` -> ⚠️ Access Error\n"
        await query.message.edit_text(stats, reply_markup=get_main_keyboard(user_id))

    elif data == "btn_deploy":
        best_runner = get_best_runner(user_id)
        if not best_runner: return await query.answer("No free runners available!", show_alert=True)
        _, repo, _ = best_runner
        try:
            repo.create_dispatch_event("deploy_bot")
            msg = await query.message.reply_text(f"🚀 **Dispatched to `{repo.full_name}`!**\nPolling for status...")
            asyncio.create_task(poll_deployment_status(client, user_id, repo, msg.id))
        except Exception as e:
            await query.message.reply_text(f"❌ Deploy failed: {e}")

    elif data == "btn_stop":
        repos_list = ACCOUNTS_DATA.get(str(user_id), [])
        if not repos_list: return await query.answer("No repos linked!", show_alert=True)
        count = 0
        for rd in repos_list:
            try:
                gh = Github(auth=Auth.Token(rd["token"]))
                repo = gh.get_repo(parse_repo_branch(rd["repo"])[0])
                runs = repo.get_workflow_runs(status="in_progress")
                for run in runs:
                    run.cancel()
                    count += 1
            except: pass
        await query.answer(f"🛑 Cancelled {count} active runners across the pool!", show_alert=True)

    elif data == "btn_edit_env":
        USER_STATE[user_id].update({"action": "wait_env", "timestamp": time.time()})
        await query.message.edit_text("🔧 Send Env Vars in format:\n`KEY=VALUE`\n(This will save to ALL repos in your pool)", reply_markup=get_cancel_keyboard())

# ================= UPLOAD MANAGER =================
@app.on_message(filters.document)
async def handle_document(client, message):
    user_id = message.from_user.id
    if str(user_id) not in ACCOUNTS_DATA or not ACCOUNTS_DATA[str(user_id)]:
        return await message.reply_text("❌ Please add at least one GitHub account to your runner pool via /start")
    
    doc = message.document
    file_ext = doc.file_name.split(".")[-1].lower()
    if file_ext not in ["py", "js", "zip"]: return await message.reply_text("❌ Only `.py`, `.js`, or `.zip` allowed!")

    status = await message.reply_text("📥 Downloading...")
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

    entry_file = detect_entry_file(bot_dir)
    USER_STATE[user_id] = {"dir": bot_dir, "timestamp": time.time()}

    if not entry_file:
        if file_ext in ["py", "js"]: entry_file = doc.file_name
        else:
            USER_STATE[user_id]["action"] = "wait_entry"
            return await status.edit_text("🚨 **Main file not found!**\nSend the exact file path or run command:", reply_markup=get_cancel_keyboard())

    USER_STATE[user_id]["entry"] = entry_file
    await status.edit_text("⚙️ Selecting best runner from pool & processing files...")
    await process_and_sync(client, user_id, status)

# ================= TEXT STATE HANDLER =================
@app.on_message(filters.text & ~filters.command(["start", "cancel"]))
async def text_handler(client, message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id)
    text = message.text.strip()
    if not state or "action" not in state: return
    if "timestamp" in state and (time.time() - state["timestamp"] > 300):
        cleanup_state(user_id)
        return await message.reply_text("⏰ Session expired.", reply_markup=get_main_keyboard(user_id))

    action = state["action"]
    if action == "wait_token":
        USER_STATE[user_id]["temp_token"] = text
        USER_STATE[user_id].update({"action": "wait_repo", "timestamp": time.time()})
        await message.reply_text("📁 Now send your GitHub Repo name.\nFormat: `username/repo` or `username/repo:branch`", reply_markup=get_cancel_keyboard())

    elif action == "wait_repo":
        token = state.get("temp_token")
        repo_str = text
        repo_name, branch = parse_repo_branch(repo_str)
        status = await message.reply_text("🔍 Validating repository...")
        try:
            gh = Github(auth=Auth.Token(token))
            repo = gh.get_repo(repo_name)
            
            # FEATURE 1: List Update (Multi-repo)
            if str(user_id) not in ACCOUNTS_DATA: ACCOUNTS_DATA[str(user_id)] = []
            ACCOUNTS_DATA[str(user_id)].append({"token": token, "repo": repo_str})
            save_accounts()
            
            cleanup_state(user_id)
            await status.edit_text(f"✅ Runner Repo `{repo_str}` added to your pool!", reply_markup=get_main_keyboard(user_id))
        except GithubException as e:
            await status.edit_text(f"❌ Error validating: {e}", reply_markup=get_cancel_keyboard())

    elif action == "wait_entry":
        USER_STATE[user_id]["entry"] = text 
        status = await message.reply_text("⚙️ Selecting runner & Syncing files...")
        await process_and_sync(client, user_id, status)

    elif action == "wait_env":
        repos_list = ACCOUNTS_DATA.get(str(user_id), [])
        status = await message.reply_text("🔐 Updating Secrets across all pool repos...")
        success = 0
        for rd in repos_list:
            try:
                gh = Github(auth=Auth.Token(rd["token"]))
                repo = gh.get_repo(parse_repo_branch(rd["repo"])[0])
                lines = text.split("\n")
                for line in lines:
                    if "=" in line:
                        k, v = line.split("=", 1)
                        update_gh_secret(repo, k.strip().upper(), v.strip())
                success += 1
            except: pass
        cleanup_state(user_id)
        await status.edit_text(f"✅ Secrets updated securely on {success}/{len(repos_list)} runners!", reply_markup=get_main_keyboard(user_id))

if __name__ == "__main__":
    print("🚀 Enterprise Manager Bot is Starting...")
    app.run()