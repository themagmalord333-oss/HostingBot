import os, shutil, zipfile, asyncio, uuid, base64, json
from github import Github, InputGitTreeElement
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
import config

app = Client("BugFreeHost", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)
USER_STATE = {}
ACCOUNTS_FILE = "accounts.json"

# ================= ACCOUNTS MANAGER =================
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, "r") as f: return json.load(f)
    return []

def save_account(token, repo):
    accs = load_accounts()
    accs.append({"token": token, "repo": repo})
    with open(ACCOUNTS_FILE, "w") as f: json.dump(accs, f)

# ================= UI & KEYBOARDS =================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add GH Account", callback_data="btn_add_acc"), InlineKeyboardButton("📊 Runner Pool", callback_data="btn_pool")],
        [InlineKeyboardButton("📄 Upload .PY", callback_data="btn_py"), InlineKeyboardButton("📦 Upload .ZIP", callback_data="btn_zip")],
        [InlineKeyboardButton("🔑 Add Secrets", callback_data="btn_env"), InlineKeyboardButton("⚙️ Custom Cmd", callback_data="btn_cmd")],
        [InlineKeyboardButton("🚀 DEPLOY BOT (Smart)", callback_data="btn_deploy")],
        [InlineKeyboardButton("🔴 STOP ALL RUNNERS", callback_data="btn_stop")],
    ])

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="btn_cancel")]])

# ================= MAIN LOGIC =================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text("<b>👑 BUG-FREE CLOUD HOSTING MANAGER</b>\n\nManage Multiple GitHub Runners & Deploy Smartly.", reply_markup=get_main_keyboard())

@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id

    if data == "btn_cancel":
        USER_STATE.pop(user_id, None)
        return await query.message.edit_text("❌ **Cancelled!**", reply_markup=get_main_keyboard())

    # ADD NEW ACCOUNT LOGIC
    if data == "btn_add_acc":
        USER_STATE[user_id] = {"action": "wait_token"}
        await query.message.edit_text("➕ **Send GitHub Token:**\n(Must have repo & workflow permissions)", reply_markup=get_cancel_keyboard())

    # VIEW RUNNER POOL LOGIC
    elif data == "btn_pool":
        accs = load_accounts()
        if not accs: return await query.message.edit_text("⚠️ No accounts added yet!", reply_markup=get_main_keyboard())

        text = "📊 **Runner Pool Status:**\n\n"
        await query.message.edit_text("⏳ Fetching pool status...")

        for i, acc in enumerate(accs):
            try:
                gh = Github(acc["token"])
                repo = gh.get_repo(acc["repo"])
                runs = repo.get_workflow_runs(status="in_progress")
                active = runs.totalCount
                status = "🔴 Busy" if active > 0 else "🟢 Available"
                text += f"**{i+1}.** `{acc['repo']}` - {status} ({active} runs)\n"
            except Exception as e:
                text += f"**{i+1}.** `{acc['repo']}` - ❌ Error/Invalid Token\n"

        await query.message.edit_text(text, reply_markup=get_main_keyboard())

    # SMART DEPLOY LOGIC (LOAD BALANCER)
    elif data == "btn_deploy":
        accs = load_accounts()
        if not accs: return await query.message.edit_text("⚠️ Add an account first!", reply_markup=get_main_keyboard())

        await query.message.edit_text("⏳ Finding an available runner...")

        deployed = False
        for acc in accs:
            try:
                gh = Github(acc["token"])
                repo = gh.get_repo(acc["repo"])
                runs = repo.get_workflow_runs(status="in_progress")

                if runs.totalCount == 0:  # Free runner found!
                    repo.create_dispatch_event("run-hosted-bot")
                    await query.message.edit_text(f"✅ **Deployed Successfully!**\n🚀 Runner picked: `{acc['repo']}`", reply_markup=get_main_keyboard())
                    deployed = True
                    break
            except Exception as e:
                continue

        if not deployed:
            await query.message.edit_text("⚠️ **All runners are currently busy!**\nTry adding more GitHub accounts to the pool.", reply_markup=get_main_keyboard())

    # STOP ALL LOGIC
    elif data == "btn_stop":
        await query.message.edit_text("🔴 Stopping all active runners across pool...")
        accs = load_accounts()
        count = 0
        for acc in accs:
            try:
                gh = Github(acc["token"])
                repo = gh.get_repo(acc["repo"])
                runs = repo.get_workflow_runs(status="in_progress")
                for run in runs:
                    run.cancel()
                    count += 1
            except: pass
        await query.message.edit_text(f"🛑 **Successfully Stopped {count} Instances!**", reply_markup=get_main_keyboard())

    # FILE UPLOADS & SECRETS (Pushes to ALL repos in pool to keep them synced)
    elif data in ["btn_py", "btn_zip", "btn_env", "btn_cmd"]:
        USER_STATE[user_id] = {"action": data}
        prompts = {
            "btn_py": "📄 **Send `.py` file:** (Syncs to all runners)",
            "btn_zip": "📦 **Send `.zip`:** (Syncs to all runners)",
            "btn_env": "🔑 **Send ENV Variables:**\n`KEY=VALUE`",
            "btn_cmd": "⚙️ **Send Custom Start Command:**"
        }
        await query.message.edit_text(prompts[data], reply_markup=get_cancel_keyboard())

@app.on_message(filters.text & ~filters.command(["start"]))
async def text_handler(client, message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id)
    if not state: return
    action = state.get("action")

    if action == "wait_token":
        USER_STATE[user_id] = {"action": "wait_repo", "token": message.text.strip()}
        await message.reply_text("✅ Token received.\n\n➕ **Now send the Repository Name:**\n(e.g., `username/repo-name`)", reply_markup=get_cancel_keyboard())

    elif action == "wait_repo":
        token = state.get("token")
        repo = message.text.strip()
        save_account(token, repo)
        USER_STATE.pop(user_id, None)
        await message.reply_text(f"✅ **Account Added to Pool!**\nRepo: `{repo}`", reply_markup=get_main_keyboard())

# (Here you will add the upload logic for btn_py, btn_zip, btn_env...
# Basically wrap your old upload logic inside a `for acc in load_accounts():` loop
# so that the code/secrets get uploaded to ALL accounts in the pool automatically!)

if __name__ == "__main__":
    print("🚀 Master Load Balancer is Online!")
    app.run()