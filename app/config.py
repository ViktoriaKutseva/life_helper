from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class EnvVars(BaseSettings):
    TELEGRAM_BOT_TOKEN: str
    DB_URL: str
    ADMIN_CHAT_ID: str = ""  # Optional admin chat ID for startup notification

    model_config = SettingsConfigDict(env_file='.env', extra="ignore")

# Function to get environment variables
def get_env_vars() -> EnvVars:
    return EnvVars()
