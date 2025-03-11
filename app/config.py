from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

# Access variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Ensure the token exists
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing from .env file")
