import os

API_ID = int(os.getenv("API_ID", "1234567"))
API_HASH = os.getenv("API_HASH", "YOUR_API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "ghp_xxxxxxxxxxxxxxx")
GITHUB_REPO = os.getenv("GITHUB_REPO", "username/target-runner-repo")

MAX_FILE_SIZE = 20 * 1024 * 1024 # 20 MB Upload Limit
MAX_EXTRACT_SIZE = 500 * 1024 * 1024 # 500 MB Unzipped Limit (Anti-ZIP Bomb)