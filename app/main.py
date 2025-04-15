from config import get_env_vars
from bot import TgBotClient

if __name__ == "__main__":
    
    bot = TgBotClient(get_env_vars().TELEGRAM_BOT_TOKEN, get_env_vars().DB_URL)
    bot.run()
