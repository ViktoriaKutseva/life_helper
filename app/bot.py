from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, ContextTypes, CallbackQueryHandler, JobQueue
from sqlalchemy.orm import sessionmaker
from loguru import logger
from datetime import datetime, time
from functools import partial
from pytz import timezone
import asyncio

from database.database import engine, SessionLocal
from services.crud import create_user, get_user_by_telegram_id, create_task, get_tasks_by_user
from config import get_env_vars
from models.models import User, Task
from enums.frequency import Frequency



class TgBotClient:
    def __init__(self, token: str, db_url: str):
        self.db_client = db_url  # Теперь используется
        self._bot: Application = (
            Application.builder()
            .token(token)
            .post_init(self.post_init)
            .build()
        )
        
        self._set_commands(self._bot)

    def _set_commands(self, application: Application) -> None:
        logger.info("Setting up commands...")
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("add_task", self.add_task_command))
        application.add_handler(CallbackQueryHandler(self.handle_button_click))
        application.add_handler(CommandHandler("list_task", self.show_all_tasks))


    async def post_init(self, application: Application) -> None:
        await application.bot.set_my_commands([
            BotCommand("start", "Начало работы с ботом"),
            BotCommand("add_task", "Добавить задачу"),
            BotCommand("list_task", "Показать все задачи")
        ])
        chat_id = "624165496"
        try:
            await application.bot.send_message(chat_id=chat_id, text="Бот запущен и готов к работе!")
            logger.info("Startup message sent successfully.")
        except Exception as e:
            logger.error(f"Failed to send startup message: {e}")
            
    async def get_username_by_id(self, bot, user_id: int):
        try:
            chat = await bot.get_chat(user_id)
            return chat.username if chat.username else chat.first_name
        except Exception as e:
            logger.error(f"Ошибка при получении имени пользователя: {e}")
            return None
   
    async def start_command(self, update, context):
        logger.info("Start adding user")
        
        chat_id = update.effective_chat.id
        db = SessionLocal()

        try:
            user = get_user_by_telegram_id(db, chat_id)
            bot = context.bot  # Получаем объект бота
            username = await self.get_username_by_id(bot, chat_id)  # Вызываем метод корректно
            
            logger.info("Getting user's username by id")

            if not user:
                user = create_user(db, chat_id)
                await update.message.reply_text(f"Вы успешно зарегистрированы, {username}! 🎉")
                logger.info("User registered successfully")
            else:
                await update.message.reply_text(f"Вы уже зарегистрированы, {username}! 😊")
                logger.info("User is already in the database")
        except Exception as e:
            logger.error(f"Ошибка в start_command: {e}")
            await update.message.reply_text("Произошла ошибка, попробуйте позже.")
        finally:
            db.close()
            
    async def add_task_command(self, update: Update, context: CallbackContext) -> None:
        """Команда для добавления задачи - ждет название и предлагает выбрать частоту."""
        if not context.args:
            await update.message.reply_text("Пожалуйста, укажите название задачи после /add_task.")
            return

        task_name = " ".join(context.args) 
        context.user_data["task_name"] = task_name 

        keyboard = [
            [InlineKeyboardButton(freq.name, callback_data=f"frequency_{freq.name}")]
            for freq in Frequency
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"Вы выбрали задачу: {task_name}\nТеперь выберите частоту:", reply_markup=reply_markup)

    async def handle_button_click(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает выбор частоты и сохраняет задачу в БД."""
        query = update.callback_query
        await query.answer()

        chat_id = query.message.chat_id
        db = SessionLocal()
        user = get_user_by_telegram_id(db, chat_id)
        
        if not user:
            await query.message.reply_text("Сначала зарегистрируйтесь с помощью /start.")
            db.close()
            return

        if query.data.startswith("frequency_"):
            frequency = query.data.split("_")[1]
            task_name = context.user_data.get("task_name")

            if not task_name:
                await query.message.reply_text("Ошибка: не найдено название задачи.")
                db.close()
                return

            create_task(
                db=db,
                user_id=user.id,
                title=task_name,
                frequency=frequency
            )

            await query.message.reply_text(f"Задача '{task_name}' с частотой '{frequency}' добавлена ✅")
        
        db.close()
        
        
    async def show_all_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            """Отправляет список всех задач пользователя"""
            db = SessionLocal()
            user_id = update.effective_user.id  # Теперь берем user_id

            try:
                user = get_user_by_telegram_id(db, user_id)
                if not user:
                    await update.message.reply_text("Вы не зарегистрированы. Используйте /start.")
                    return

                user_tasks = get_tasks_by_user(db, user.id)  # Получаем задачи по user.id

                if not user_tasks:
                    await update.message.reply_text("📭 У вас пока нет задач.")
                    return

                task_list = "\n".join(
                    [f"🔹 {task.title} ({task.frequency.name}) {'✅' if task.completed else '❌'}"
                    for task in user_tasks]
                )

                await update.message.reply_text(f"📌 Ваши задачи:\n{task_list}")
            except Exception as e:
                logger.error(f"Ошибка при получении списка задач: {e}")
                await update.message.reply_text("⚠️ Ошибка при получении списка задач.")
            finally:
                db.close()

    def run(self):
        logger.info("Starting bot")
        self._bot.run_polling()