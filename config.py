import os

# ================= TELEGRAM CREDENTIALS =================
API_ID = int(os.getenv("API_ID", "1234567"))
API_HASH = os.getenv("API_HASH", "YOUR_API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")

# ================= GEMINI AI CREDENTIALS =================
# 🔥 AI Zip Scanner ko chalne ke liye ye key zaroori hai 🔥
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")

# ================= SECURITY LIMITS =================
MAX_FILE_SIZE = 20 * 1024 * 1024 # 20 MB Upload Limit
MAX_EXTRACT_SIZE = 500 * 1024 * 1024 # 500 MB Unzipped Limit (Anti-ZIP Bomb)