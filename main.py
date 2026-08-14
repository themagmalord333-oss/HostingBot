import os, shutil, zipfile, asyncio, uuid, base64, json, ast, sys
from github import Github, InputGitTreeElement
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
import config

app = Client("BugFreeHost", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)
USER_STATE = {}
ACCOUNTS_FILE = "accounts.json"
TEMP_DIR = "temp_uploads"

os.makedirs(TEMP_DIR, exist_ok=True)

# ================= ACCOUNTS MANAGER =================
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, "r") as f: return json.load(f)
    return []

def save_account(token, repo):
    accs = load_accounts()
    accs.append({"token": token, "repo": repo})
    with open(ACCOUNTS_FILE, "w") as f: json.dump(accs, f)

# ================= SECURITY & AST HELPERS =================
def safe_extract_zip(zip_path, extract_to):
    """Zip Slip / Path Traversal Attack Proof Extraction"""
    abs_extract_to = os.path.abspath(extract_to)
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        for member in zip_ref.namelist():
            member_path = os.path.abspath(os.path.join(extract_to, member))
            if not member_path.startswith(abs_extract_to):
                raise ValueError(f"🚨 Path Traversal Detected: {member}")
        zip_ref.extractall(extract_to)

def parse_missing_imports(bot_dir):
    """Reads .py files and finds missing PIP packages"""
    std_libs = set(sys.builtin_module_names) | set(getattr(sys, "stdlib_module_names", []))
    pypi_mapping = {"PIL": "Pillow", "telegram": "python-telegram-bot", "cv2": "opencv-python", "dotenv": "python-dotenv", "dns": "dnspython", "bs4": "beautifulsoup4"}
    
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
    """Recursively search for main files"""
    std_files = ["bot.py", "main.py", "app.py", "index.js", "server.js"]
    for root, _, files in os.walk(bot_dir):
        for file in files:
            if file.lower() in std_files:
                return os.path.relpath(os.path.join(root, file), bot_dir)
    return None

# ================= GITHUB PUSH MANAGER =================
def push_folder_to_github(acc, local_dir, commit_msg="Update Bot Files"):
    """Pushes a local folder to GitHub Repo securely"""
    try:
        gh = Github(acc["token"])
        repo = gh.get_repo(acc["repo"])
        
        # Get default branch
        branch = repo.default_branch
        master_ref = repo.get_git_ref(f'heads/{branch}')
        master_sha = master_ref.object.sha
        base_tree = repo.get_git_tree(master_sha)

        elements = []
        for root, dirs, files in os.walk(local_dir):
            for file in files:
                path = os.path.join(root, file)
                rel_path = os.path.relpath(path, local_dir).replace("\\", "/")
                
                with open(path, 'rb') as f:
                    content = f.read()
                
                # Push as base64 to avoid encoding issues with zips/images
                blob = repo.create_git_blob(base64.b64encode(content).decode('utf-8'), "base64")
                elements.append(InputGitTreeElement(rel_path, '100644', 'blob', sha=blob.sha))

        if not elements: return False

        tree = repo.create_git_tree(elements, base_tree)
        parent = repo.get_git_commit(master_sha)
        commit = repo.create_git_commit(commit_msg, tree, [parent])
        master_ref.edit(commit.sha)
        return True
    except Exception as e:
        print(f"GH Push Error on {acc['repo']}: {e}")
        return False

def update_gh_secret(acc, secret_name, secret_value):
    try:
        gh = Github(acc["token"])
        repo = gh.get_repo(acc["repo"])
        repo.create_secret(secret_name, secret_value)
    except: pass

# ================= UI & KEYBOARDS =================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add GH Account", callback_data="btn_add_acc"), InlineKeyboardButton("📊 Runner Pool", callback_data="btn_pool")],
        [InlineKeyboardButton("📄 File Uploads", callback_data="dummy"), InlineKeyboardButton("📦 Install Pkgs", callback_data="btn_pkg")],
        [InlineKeyboardButton("🔑 Add Secrets", callback_data="btn_env"), InlineKeyboardButton("📂 Set Entry", callback_data="btn_entry")],
        [InlineKeyboardButton("🚀 DEPLOY BOT (Smart)", callback_data="btn_deploy")],
        [InlineKeyboardButton("🔴 STOP ALL RUNNERS", callback_data="btn_stop")],
    ])

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="btn_cancel")]])

# ================= COMMAND HANDLERS =================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text("<b>👑 BUG-FREE CLOUD HOSTING MANAGER</b>\n\nUpload `.py` or `.zip`. Bot handles dependencies & GitHub sync automatically.", reply_markup=get_main_keyboard())

# ================= FILE UPLOAD (ZIP, PY, JS) =================
@app.on_message(filters.document)
async def handle_document(client, message):
    user_id = message.from_user.id
    doc = message.document
    file_ext = doc.file_name.split(".")[-1].lower()

    if file_ext not in ["py", "js", "zip"]:
        return await message.reply_text("❌ Only `.py`, `.js`, or `.zip` allowed!")

    accs = load_accounts()
    if not accs:
        return await message.reply_text("⚠️ Please add a GitHub account first!")

    status = await message.reply_text("📥 Downloading file...")
    
    session_id = str(uuid.uuid4())
    bot_dir = os.path.join(TEMP_DIR, session_id)
    os.makedirs(bot_dir, exist_ok=True)
    
    file_path = os.path.join(bot_dir, doc.file_name)
    await message.download(file_path)

    # 1. ZIP Extraction (Safe)
    if file_ext == "zip":
        await status.edit_text("📦 Extracting ZIP securely...")
        try:
            safe_extract_zip(file_path, bot_dir)
            os.remove(file_path)
        except Exception as e:
            shutil.rmtree(bot_dir)
            return await status.edit_text(f"❌ Zip Extract Error: {e}")

    # 2. Dependency Generation (AST)
    req_path = os.path.join(bot_dir, "requirements.txt")
    if not os.path.exists(req_path):
        await status.edit_text("🔍 Scanning code for missing PIP packages...")
        pkgs = parse_missing_imports(bot_dir)
        if pkgs:
            with open(req_path, "w") as f:
                f.write("\n".join(pkgs))

    # 3. Entry File Detection
    await status.edit_text("🔍 Detecting Entry File...")
    entry_file = detect_entry_file(bot_dir)

    # If Entry File NOT FOUND -> Save state, Trigger Manual Fallback
    if not entry_file:
        USER_STATE[user_id] = {"action": "wait_entry", "dir": bot_dir}
        return await status.edit_text(
            "🚨 **Entry file not found automatically!**\n\n"
            "Please send the relative path of your main file.\n"
            "(Example: `src/main.py` or `bot.js`)", 
            reply_markup=get_cancel_keyboard()
        )

    # If Entry Found -> Sync to GitHub
    await process_and_sync(user_id, bot_dir, entry_file, status)

async def process_and_sync(user_id, bot_dir, entry_file, status_msg):
    """Pushes local folder to all GitHub repos and sets RUN_COMMAND"""
    await status_msg.edit_text("☁️ Syncing code to GitHub Pool...")
    
    accs = load_accounts()
    sync_count = 0

    cmd = f"python3 {entry_file}" if entry_file.endswith(".py") else f"node {entry_file}"

    for acc in accs:
        # Push Files
        success = push_folder_to_github(acc, bot_dir)
        if success:
            # Update GitHub Secret for Custom Command
            update_gh_secret(acc, "RUN_COMMAND", cmd)
            sync_count += 1

    shutil.rmtree(bot_dir)
    await status_msg.edit_text(f"✅ **Synced successfully to {sync_count} repos!**\n📂 Entry: `{entry_file}`\n\nPress **DEPLOY** to start.", reply_markup=get_main_keyboard())


# ================= CALLBACKS =================
@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id

    if data == "btn_cancel":
        USER_STATE.pop(user_id, None)
        return await query.message.edit_text("❌ **Cancelled!**", reply_markup=get_main_keyboard())

    if data == "btn_add_acc":
        USER_STATE[user_id] = {"action": "wait_token"}
        await query.message.edit_text("➕ **Send GitHub Token:**", reply_markup=get_cancel_keyboard())

    elif data == "btn_pool":
        accs = load_accounts()
        if not accs: return await query.message.edit_text("⚠️ No accounts added!", reply_markup=get_main_keyboard())
        text = "📊 **Runner Pool Status:**\n\n"
        for i, acc in enumerate(accs):
            try:
                gh = Github(acc["token"])
                repo = gh.get_repo(acc["repo"])
                active = repo.get_workflow_runs(status="in_progress").totalCount
                status = "🔴 Busy" if active > 0 else "🟢 Available"
                text += f"**{i+1}.** `{acc['repo']}` - {status} ({active} runs)\n"
            except: text += f"**{i+1}.** `{acc['repo']}` - ❌ Error\n"
        await query.message.edit_text(text, reply_markup=get_main_keyboard())

    elif data == "btn_deploy":
        accs = load_accounts()
        if not accs: return await query.message.edit_text("⚠️ Add an account first!", reply_markup=get_main_keyboard())
        await query.message.edit_text("⏳ Finding an available runner...")
        for acc in accs:
            try:
                repo = Github(acc["token"]).get_repo(acc["repo"])
                if repo.get_workflow_runs(status="in_progress").totalCount == 0:
                    repo.create_dispatch_event("run-hosted-bot")
                    return await query.message.edit_text(f"✅ **Deployed!**\n🚀 Runner: `{acc['repo']}`", reply_markup=get_main_keyboard())
            except: continue
        await query.message.edit_text("⚠️ **All runners are busy!**", reply_markup=get_main_keyboard())

    elif data == "btn_stop":
        await query.message.edit_text("🔴 Stopping all runners...")
        for acc in load_accounts():
            try:
                for run in Github(acc["token"]).get_repo(acc["repo"]).get_workflow_runs(status="in_progress"):
                    run.cancel()
            except: pass
        await query.message.edit_text("🛑 **Stopped All Instances!**", reply_markup=get_main_keyboard())

    elif data == "btn_env":
        USER_STATE[user_id] = {"action": "wait_env"}
        await query.message.edit_text("🔑 **Send ENV Variable:**\nFormat: `KEY=VALUE`", reply_markup=get_cancel_keyboard())
        
    elif data == "btn_entry":
        USER_STATE[user_id] = {"action": "wait_entry_only"}
        await query.message.edit_text("📂 **Send Custom Entry File Path:**\n(Example: `main.py`)", reply_markup=get_cancel_keyboard())
        
    elif data == "btn_pkg":
        USER_STATE[user_id] = {"action": "wait_pkg"}
        await query.message.edit_text("📦 **Send packages to install:**\n(Example: `requests aiohttp`)", reply_markup=get_cancel_keyboard())


# ================= TEXT INPUT HANDLERS =================
@app.on_message(filters.text & ~filters.command(["start"]))
async def text_handler(client, message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id)
    if not state: return
    action = state.get("action")

    if action == "wait_token":
        USER_STATE[user_id] = {"action": "wait_repo", "token": message.text.strip()}
        await message.reply_text("✅ Token received.\n➕ **Send Repo Name:** (e.g., `user/repo`)", reply_markup=get_cancel_keyboard())

    elif action == "wait_repo":
        save_account(state["token"], message.text.strip())
        USER_STATE.pop(user_id, None)
        await message.reply_text("✅ **Account Added!**", reply_markup=get_main_keyboard())

    # 🚨 NEW: Manual Entry File Fallback Logic
    elif action == "wait_entry":
        bot_dir = state["dir"]
        user_path = message.text.strip()
        safe_path = os.path.abspath(os.path.join(bot_dir, user_path))

        # Path Traversal & Existence Check
        if safe_path.startswith(os.path.abspath(bot_dir)) and os.path.isfile(safe_path):
            status = await message.reply_text("✅ File validated! Syncing to GitHub...")
            USER_STATE.pop(user_id, None)
            await process_and_sync(user_id, bot_dir, user_path, status)
        else:
            await message.reply_text("❌ File not found! Please check spelling and send relative path again:")

    # 🚨 NEW: Standalone Set Entry Logic
    elif action == "wait_entry_only":
        cmd_path = message.text.strip()
        cmd = f"python3 {cmd_path}" if cmd_path.endswith(".py") else f"node {cmd_path}"
        
        status = await message.reply_text("🔄 Syncing RUN_COMMAND to all repos...")
        for acc in load_accounts():
            update_gh_secret(acc, "RUN_COMMAND", cmd)
            
        USER_STATE.pop(user_id, None)
        await status.edit_text(f"✅ **Entry File Updated to:** `{cmd_path}`", reply_markup=get_main_keyboard())

    # 🚨 NEW: Standalone Install Packages Logic
    elif action == "wait_pkg":
        pkgs = message.text.strip()
        status = await message.reply_text("🔄 Updating requirements.txt on all repos...")
        
        for acc in load_accounts():
            try:
                repo = Github(acc["token"]).get_repo(acc["repo"])
                try:
                    # Get existing requirements.txt if it exists
                    file = repo.get_contents("requirements.txt")
                    new_content = base64.b64decode(file.content).decode() + f"\n{pkgs}"
                    repo.update_file("requirements.txt", "Update dependencies", new_content, file.sha)
                except:
                    # Create new requirements.txt if it doesn't exist
                    repo.create_file("requirements.txt", "Add dependencies", pkgs)
            except: pass
            
        USER_STATE.pop(user_id, None)
        await status.edit_text(f"✅ **Packages Added:** `{pkgs}`", reply_markup=get_main_keyboard())

    # ENV Variables Logic
    elif action == "wait_env":
        if "=" not in message.text:
            return await message.reply_text("❌ Invalid Format. Send `KEY=VALUE`")
        
        k, v = message.text.split("=", 1)
        status = await message.reply_text("🔄 Syncing Secret to all repos...")
        for acc in load_accounts():
            update_gh_secret(acc, k.strip(), v.strip())
            
        USER_STATE.pop(user_id, None)
        await status.edit_text(f"✅ **Secret `{k}` added to pool!**", reply_markup=get_main_keyboard())

if __name__ == "__main__":
    print("🚀 Master Load Balancer is Online!")
    app.run()