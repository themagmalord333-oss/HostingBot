import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "123456"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ================= SCALER & CLOUD VARIABLES =================
MONGO_URI = os.getenv("MONGO_URI", "")
GH_PERSONAL_TOKEN = os.getenv("GH_PERSONAL_TOKEN", "")

# GitHub Secret ka actual naam REPO_NAME rahega
GITHUB_REPO = os.getenv("REPO_NAME", "").strip()

WORKFLOW_FILE = os.getenv("WORKFLOW_FILE", "main.yml").strip()