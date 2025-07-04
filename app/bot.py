from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, ContextTypes, CallbackQueryHandler, JobQueue, MessageHandler, filters
from sqlalchemy.orm import sessionmaker
from loguru import logger
from datetime import datetime, time, timedelta
from functools import partial
from pytz import timezone
import asyncio
import sqlalchemy
from sqlalchemy import exc  # Add explicit import for sqlalchemy.exc
import re

from database.database import engine, SessionLocal
from services.crud import (
    create_user, get_user_by_telegram_id, create_task, get_tasks_by_user,
    get_all_users, get_tasks_due_today, complete_task, reset_recurring_tasks,
    update_user_last_notified, delete_task, schedule_yearly_cleanup, is_task_completed_today
)
from config import get_env_vars
from models.models import User, Task
from enums.frequency import Frequency


DAYS_OF_WEEK = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

class TgBotClient:
    def __init__(self, token: str, db_url: str):
        self.db_client = db_url  # Теперь используется
        # Explicitly create and pass the JobQueue
        job_queue = JobQueue()
        self._bot: Application = (
            Application.builder()
            .token(token)
            .job_queue(job_queue) # Pass the created job_queue
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
        application.add_handler(CommandHandler("today_tasks", self.today_tasks_command))
        application.add_handler(CommandHandler("done", self.done_command))
        application.add_handler(CommandHandler("delete", self.delete_command))
        application.add_handler(CommandHandler("points", self.points_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message_handler))


    async def post_init(self, application: Application) -> None:
        await application.bot.set_my_commands([
            BotCommand("start", "Начало работы с ботом"),
            BotCommand("add_task", "Добавить задачу"),
            BotCommand("list_task", "Показать все задачи"),
            BotCommand("today_tasks", "Задачи на сегодня"),
            BotCommand("done", "Отметить задачу как выполненную: /done <ID задачи>"),
            BotCommand("delete", "Удалить задачу: /delete <ID задачи>"),
            BotCommand("points", "Показать общее количество баллов пользователя")
        ])
        
        # Get admin chat ID from environment variables
        env_vars = get_env_vars()
        admin_chat_id = env_vars.ADMIN_CHAT_ID
        
        # Only send startup notification if admin chat ID is set
        if admin_chat_id:
            try:
                await application.bot.send_message(chat_id=admin_chat_id, text="Бот запущен и готов к работе!")
                logger.info(f"Startup message sent to admin {admin_chat_id}")
            except Exception as e:
                logger.error(f"Failed to send startup message: {e}")
        else:
            logger.info("No admin chat ID set, skipping startup notification")
            
        # Schedule the daily reminder job
        job_queue = application.job_queue
        
        # Set the time to specified Yekaterinburg time (UTC+5)
        target_time = time(9, 0, 0, tzinfo=timezone('Asia/Yekaterinburg'))
        job_queue.run_daily(self.send_daily_reminders, target_time)
        logger.info(f"Scheduled daily reminders for {target_time} (Yekaterinburg time)")
        
        # Schedule yearly cleanup on the first day of each month
        first_day_midnight = time(0, 0, 0, tzinfo=timezone('Asia/Yekaterinburg'))
        job_queue.run_monthly(self.yearly_cleanup_job, first_day_midnight, 1)
        logger.info(f"Scheduled yearly cleanup on first day of each month at {first_day_midnight} (Yekaterinburg time)")
        
        # Schedule backup notifications every 6 hours
        job_queue.run_repeating(
            self.send_backup_reminders, 
            interval=timedelta(hours=6), 
            first=timedelta(hours=6)
        )
        logger.info("Scheduled backup reminders every 6 hours")

        await self.schedule_task_reminders(application)

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
        context.user_data["selected_days"] = set() # Initialize selected days set

        keyboard = [
            [InlineKeyboardButton(freq.name, callback_data=f"frequency_{freq.name}")]
            for freq in Frequency
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"Вы выбрали задачу: {task_name}\nТеперь выберите частоту:", reply_markup=reply_markup)

    def _build_day_selection_keyboard(self, selected_days: set) -> list[list[InlineKeyboardButton]]:
        """Helper method to build the day selection keyboard."""
        keyboard = []
        row = []
        for day in DAYS_OF_WEEK:
            text = f"{'✅ ' if day in selected_days else ''}{day}"
            callback_data = f"day_select_{day}"
            row.append(InlineKeyboardButton(text, callback_data=callback_data))
            if len(row) == 3: # 3 buttons per row
                keyboard.append(row)
                row = []
        if row: # Add remaining buttons if any
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("Done", callback_data="day_done")])
        return keyboard


    async def handle_button_click(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает выбор частоты (включая дни недели) и сохраняет задачу в БД."""
        query = update.callback_query
        await query.answer()

        chat_id = query.message.chat_id
        db = SessionLocal()
        user = get_user_by_telegram_id(db, chat_id)

        if not user:
            await query.edit_message_text("Сначала зарегистрируйтесь с помощью /start.")
            db.close()
            return

        callback_data = query.data
            
        # Check if this is a task completion button
        if callback_data.startswith("complete_"):
            try:
                task_id = int(callback_data.split("_")[1])
                
                # Verify task belongs to user
                task = db.query(Task).filter(Task.id == task_id, Task.user_id == user.id).first()
                
                if not task:
                    await query.edit_message_text("Задача не найдена или не принадлежит вам.")
                    db.close()
                    return
                    
                # Check if already completed today
                if is_task_completed_today(db, task):
                    await query.edit_message_text("Эта задача уже отмечена как выполненная сегодня. Обновите список командой /list_task")
                    db.close()
                    return
                    
                # Mark as completed
                task = complete_task(db, task_id)
                
                # Set the task as completed (for display purposes) - this gets overridden by get_tasks_by_user later
                task.completed = True
                
                # Update the message to reflect the change
                user_tasks = get_tasks_by_user(db, user.id)
                new_message, new_markup = await self._format_task_list(user_tasks, "📌 Ваши задачи:", with_buttons=True)
                
                try:
                    await query.edit_message_text(new_message, reply_markup=new_markup)
                except Exception as e:
                    # Handle the case where message content hasn't changed (common with new completion tracking)
                    if "Message is not modified" in str(e):
                        logger.info(f"Message wasn't modified when completing task #{task_id} - this is normal with completion tracking")
                    else:
                        # For other errors, log them but continue so we can at least send the confirmation
                        logger.error(f"Error updating message after task completion: {e}")
                
                # Send a separate confirmation message to the user who completed the task
                await context.bot.send_message(
                    chat_id=query.message.chat_id,  # Use the chat ID from the query
                    text=f"✅ Задача #{task_id}: '{task.title}' отмечена как выполненная!"
                )
                
                db.close()
                return
            except Exception as e:
                logger.error(f"Error in task completion button handler: {e}")
                try:
                    await query.edit_message_text("⚠️ Произошла ошибка при выполнении этого действия.")
                except Exception:
                    pass
                db.close()
                return
        
        # Check if this is a task deletion button
        if callback_data.startswith("delete_"):
            try:
                task_id = int(callback_data.split("_")[1])
                
                # Verify task belongs to user
                task = db.query(Task).filter(Task.id == task_id, Task.user_id == user.id).first()
                
                if not task:
                    await query.edit_message_text("Задача не найдена или не принадлежит вам.")
                    db.close()
                    return
                
                # Remember task name before deletion
                task_title = task.title
                
                # Delete the task
                delete_task(db, task_id)
                
                # Update the message to reflect the change
                user_tasks = get_tasks_by_user(db, user.id)
                
                if user_tasks:
                    new_message, new_markup = await self._format_task_list(user_tasks, "📌 Ваши задачи:", with_buttons=True)
                    try:
                        await query.edit_message_text(new_message, reply_markup=new_markup)
                    except Exception as e:
                        if "Message is not modified" in str(e):
                            logger.info(f"Message wasn't modified when deleting task #{task_id} - continuing anyway")
                        else:
                            logger.error(f"Error updating message after task deletion: {e}")
                else:
                    # No tasks left
                    try:
                        await query.edit_message_text("📌 Ваши задачи:\n📭 Задач нет.")
                    except Exception as e:
                        logger.error(f"Error updating message to show no tasks: {e}")
                
                # Send a separate confirmation message
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"🗑️ Задача #{task_id}: '{task_title}' удалена!"
                )
                
                db.close()
                return
            except Exception as e:
                logger.error(f"Error in task deletion button handler: {e}")
                try:
                    await query.edit_message_text("⚠️ Произошла ошибка при удалении задачи.")
                except Exception:
                    pass
                db.close()
                return

        task_name = context.user_data.get("task_name")
        selected_days = context.user_data.get("selected_days", set()) # Ensure selected_days exists

        # Check if task_name is missing (e.g., user clicks old buttons)
        if not task_name and not query.data.startswith("day_"):
             await query.edit_message_text("Ошибка: не найдено название задачи. Пожалуйста, начните сначала с /add_task.")
             db.close()
             return
        elif not task_name and query.data.startswith("day_"):
             await query.edit_message_text("Произошла ошибка с состоянием. Пожалуйста, начните сначала с /add_task.")
             # Attempt to clean up potentially inconsistent state
             context.user_data.pop("task_name", None)
             context.user_data.pop("selected_days", None)
             db.close()
             return


        try: # Wrap database operations in try/finally
            if callback_data.startswith("frequency_"):
                frequency_str = callback_data.split("_", 1)[1]
                try:
                    frequency_enum = Frequency[frequency_str]
                except KeyError:
                    logger.error(f"Invalid frequency string received: {frequency_str}")
                    await query.edit_message_text("An error occurred: invalid frequency.")
                    return
                context.user_data["frequency_enum"] = frequency_enum
                if frequency_enum == Frequency.SPECIFIC_DAYS:
                    selected_days = context.user_data.get("selected_days", set())
                    keyboard = self._build_day_selection_keyboard(selected_days)
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(
                        "Select the days for this task:",
                        reply_markup=reply_markup
                    )
                    return
                else:
                    await query.edit_message_text(
                        "Enter reminder time for the task (e.g., 09:30) or press 'Skip':",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="skip_reminder_time")]])
                    )
                    return
            elif callback_data == "day_done":
                selected_days = context.user_data.get("selected_days", set())
                if not selected_days:
                    await query.answer("You didn't select any days!", show_alert=True)
                    return
                days_str = ",".join(sorted(list(selected_days), key=DAYS_OF_WEEK.index))
                context.user_data["days_of_week"] = days_str
                await query.edit_message_text(
                    "Enter reminder time for the task (e.g., 09:30) or press 'Skip':",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="skip_reminder_time")]])
                )
                return
            elif callback_data == "skip_reminder_time":
                await query.edit_message_text("Введите количество баллов за выполнение этой задачи (целое число):")
                context.user_data["awaiting_points"] = True
                return

            elif callback_data.startswith("day_select_"):
                day = callback_data.split("_")[2]
                if day in DAYS_OF_WEEK: # Basic validation
                    if day in selected_days:
                        selected_days.remove(day)
                    else:
                        selected_days.add(day)
                    context.user_data["selected_days"] = selected_days # Update user_data

                    # Rebuild keyboard and update message
                    keyboard = self._build_day_selection_keyboard(selected_days)
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    # Use edit_message_reply_markup to avoid flickering/resending text
                    await query.edit_message_reply_markup(reply_markup=reply_markup)
                else:
                     logger.warning(f"Invalid day received in callback: {day}")
                     await query.answer("Ошибка: неверный день", show_alert=True) # Notify user

        except Exception as e:
            logger.error(f"Error in handle_button_click: {e}")
            # Try to inform the user, but avoid editing if the original message might be gone
            try:
                await query.edit_message_text("⚠️ Произошла внутренняя ошибка при обработке вашего запроса.")
            except Exception as inner_e:
                logger.error(f"Failed to send error message to user: {inner_e}")
            # Clean up potentially inconsistent state
            context.user_data.pop("task_name", None)
            context.user_data.pop("selected_days", None)
        finally:
            db.close() # Ensure DB session is closed


    async def _format_task_list(self, tasks: list[Task], title_prefix: str, with_buttons: bool = False) -> tuple[str, InlineKeyboardMarkup | None]:
        """Helper to format a list of tasks into a message string.
        
        Args:
            tasks: List of Task objects
            title_prefix: Title for the message
            with_buttons: Whether to add completion buttons
            
        Returns:
            Tuple of (message_text, markup or None)
        """
        if not tasks:
            return f"{title_prefix}\n📭 Задач нет.", None
        
        task_lines = []
        buttons = []
        
        for task in tasks:
            task_id = task.id
            freq_str = task.frequency.name
            if task.frequency == Frequency.SPECIFIC_DAYS and task.days_of_week:
                freq_str = f"Specific ({task.days_of_week})"
            elif task.frequency == Frequency.SPECIFIC_DAYS:
                freq_str = "Specific (Дни не указаны?)"
                logger.warning(f"Task {task.id} has SPECIFIC_DAYS frequency but no days_of_week set.")

            # Универсальный парсинг reminder_time
            reminder_str = ""
            if task.reminder_time:
                t = None
                if isinstance(task.reminder_time, str):
                    try:
                        t = datetime.strptime(task.reminder_time, "%H:%M:%S.%f").time()
                    except ValueError:
                        try:
                            t = datetime.strptime(task.reminder_time, "%H:%M:%S").time()
                        except Exception:
                            reminder_str = f"⏰ {task.reminder_time}"
                    if t:
                        reminder_str = f"⏰ {t.strftime('%H:%M')}"
                else:
                    reminder_str = f"⏰ {task.reminder_time.strftime('%H:%M')}"
            status = '✅' if task.completed else '❌'
            points_str = f"🏅{task.points}" if hasattr(task, 'points') else ""
            
            if with_buttons:
                if not task.completed:
                    button_row = []
                    complete_button = InlineKeyboardButton(f"✅ #{task_id}", callback_data=f"complete_{task_id}")
                    delete_button = InlineKeyboardButton(f"🗑️ #{task_id}", callback_data=f"delete_{task_id}")
                    button_row.append(complete_button)
                    button_row.append(delete_button)
                    buttons.append(button_row)
                else:
                    delete_button = InlineKeyboardButton(f"🗑️ #{task_id}", callback_data=f"delete_{task_id}")
                    buttons.append([delete_button])
            
            # Включаем reminder_str в строку задачи
            task_lines.append(f"🔹 #{task_id}: {task.title} ({freq_str}) {reminder_str} {points_str} {status}".strip())
        
        task_list = "\n".join(task_lines)
        message_text = f"{title_prefix}\n{task_list}"
        
        # Create reply markup if buttons were added
        markup = InlineKeyboardMarkup(buttons) if buttons else None
        
        return message_text, markup

    async def show_all_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Отправляет список всех задач пользователя с кнопками для отметки выполнения"""
        db = SessionLocal()
        user_id = update.effective_user.id

        try:
            user = get_user_by_telegram_id(db, user_id)
            if not user:
                await update.message.reply_text("Вы не зарегистрированы. Используйте /start.")
                return

            user_tasks = get_tasks_by_user(db, user.id)
            # Debug: log all tasks returned for this user
            logger.info(f"[DEBUG] /list_task for user {user_id}: " + str([
                {'id': t.id, 'title': t.title, 'frequency': str(t.frequency), 'reminder_time': str(t.reminder_time), 'completed': t.completed, 'user_id': t.user_id}
                for t in user_tasks
            ]))
            # Sort: tasks with reminder_time first, then by reminder_time and ID
            def sort_key(task):
                return (task.reminder_time is None, str(task.reminder_time), task.id)
            user_tasks_sorted = sorted(user_tasks, key=sort_key)
            # Use the original formatter with buttons
            message_text, markup = await self._format_task_list(user_tasks_sorted, "📌 Your tasks:", with_buttons=True)
            await update.message.reply_text(message_text, reply_markup=markup)

        except Exception as e:
            logger.error(f"Error while getting task list: {e}")
            await update.message.reply_text("⚠️ Error while getting task list.")
        finally:
            db.close()

    async def today_tasks_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handles the /today_tasks command."""
        db = SessionLocal()
        user_id = update.effective_user.id
        try:
            user = get_user_by_telegram_id(db, user_id)
            if not user:
                await update.message.reply_text("Вы не зарегистрированы. Используйте /start.")
                return

            tasks_today = get_tasks_due_today(db, user.id)
            # We only care about incomplete tasks for the /today command
            incomplete_tasks_today = [task for task in tasks_today if not task.completed]

            if not incomplete_tasks_today:
                 message_text = "🎉 Отличная работа! Нет несделанных задач на сегодня."
            else:
                # Format similar to show_all_tasks, but without completed status maybe?
                task_lines = []
                for task in incomplete_tasks_today:
                    freq_str = task.frequency.name
                    if task.frequency == Frequency.SPECIFIC_DAYS and task.days_of_week:
                        freq_str = f"({task.days_of_week})" # Shorter format for today
                    elif task.frequency in [Frequency.EVERYDAY, Frequency.WEEKLY, Frequency.MONTHLY]:
                         freq_str = f"({task.frequency.name})"
                    else: # ONCE or Specific w/o days (error case)
                         freq_str = ""

                    task_lines.append(f"🔹 {task.title} {freq_str}".strip())
                
                task_list = "\n".join(task_lines)
                message_text = f"🔔 Задачи на сегодня:\n{task_list}"

            await update.message.reply_text(message_text)

        except exc.OperationalError as e:  # Use exc.OperationalError instead of sqlalchemy.exc.OperationalError
             logger.error(f"Database error fetching today's tasks (likely SQLite DOW issue): {e}")
             # Check if the error message indicates a problem with 'dow'
             if "no such function: extract" in str(e).lower() or "dow" in str(e).lower():
                  await update.message.reply_text("⚠️ Ошибка при получении задач на сегодня. Возможно, проблема с функцией определения дня недели в SQLite. Обратитесь к администратору.")
                  # Here you might want to implement the fallback SQLite DOW logic in crud.py
             else:
                  await update.message.reply_text("⚠️ Ошибка базы данных при получении задач на сегодня.")
        except Exception as e:
            logger.error(f"Ошибка при получении задач на сегодня: {e}")
            await update.message.reply_text("⚠️ Ошибка при получении задач на сегодня.")
        finally:
            db.close()

    async def send_daily_reminders(self, context: CallbackContext) -> None:
        """Sends reminders to all users about their tasks due today."""
        logger.info("Running daily reminder job...")
        db = SessionLocal()
        try:
            users = get_all_users(db)
            if not users:
                logger.info("No registered users found for daily reminders.")
                return

            for user in users:
                try:
                    tasks_today = get_tasks_due_today(db, user.id)
                    incomplete_tasks_today = [task for task in tasks_today if not task.completed]
                    
                    if incomplete_tasks_today:
                        # Format the reminder message (similar to today_tasks_command)
                        task_lines = []
                        for task in incomplete_tasks_today:
                            freq_str = ""
                            if task.frequency == Frequency.SPECIFIC_DAYS and task.days_of_week:
                                freq_str = f"({task.days_of_week})"
                            elif task.frequency in [Frequency.EVERYDAY, Frequency.WEEKLY, Frequency.MONTHLY]:
                                freq_str = f"({task.frequency.name})"
                            
                            task_lines.append(f"🔹 {task.title} {freq_str}".strip())
                        
                        task_list = "\n".join(task_lines)
                        reminder_message = f"🔔 Доброе утро! Ваши задачи на сегодня:\n{task_list}"
                        
                        await context.bot.send_message(chat_id=user.telegram_id, text=reminder_message)
                        logger.info(f"Sent reminder to user {user.telegram_id} for {len(incomplete_tasks_today)} tasks.")
                        
                        # Update the last_notified timestamp when successful
                        update_user_last_notified(db, user.id)
                    else:
                         logger.info(f"User {user.telegram_id} has no incomplete tasks due today.")
                         # Also update last_notified even if there are no tasks
                         update_user_last_notified(db, user.id)

                except exc.OperationalError as db_err:
                    # Handle potential DB errors per user without stopping the whole job
                    logger.error(f"Database error processing reminders for user {user.telegram_id}: {db_err}")
                    # Optionally notify admin or the user about the issue
                except Exception as e:
                    # Catch errors sending message to a specific user (e.g., bot blocked)
                    logger.error(f"Failed to send reminder to user {user.telegram_id}: {e}")
                    # Consider marking user as inactive or logging repeated failures

        except Exception as e:
            # Catch broader errors like failing to get all users
            logger.error(f"Error during daily reminder job execution: {e}")
        finally:
            db.close()
            logger.info("Daily reminder job finished.")

    async def send_backup_reminders(self, context: CallbackContext) -> None:
        """Backup function to resend task reminders if they weren't sent.
        
        This runs every few hours to ensure users get their daily reminders
        even if the main job fails.
        """
        today = datetime.now(timezone('Asia/Yekaterinburg')).date()
        logger.info(f"Running backup reminder check for {today}...")
        
        db = SessionLocal()
        try:
            users = get_all_users(db)
            if not users:
                logger.info("No registered users for backup reminders.")
                return

            for user in users:
                try:
                    # Check if user has been notified today
                    if user.last_notified:
                        # Convert to Yekaterinburg timezone for comparison
                        last_notified_yekaterinburg = user.last_notified.astimezone(timezone('Asia/Yekaterinburg'))
                        if last_notified_yekaterinburg.date() == today:
                            # User already notified today, skip
                            logger.info(f"User {user.telegram_id} already notified today at {last_notified_yekaterinburg}, skipping backup.")
                            continue
                    
                    # User hasn't been notified today, check for tasks and send reminder
                    tasks_today = get_tasks_due_today(db, user.id)
                    incomplete_tasks_today = [task for task in tasks_today if not task.completed]
                    
                    if incomplete_tasks_today:
                        # Format message similar to daily reminders
                        task_lines = []
                        for task in incomplete_tasks_today:
                            freq_str = ""
                            if task.frequency == Frequency.SPECIFIC_DAYS and task.days_of_week:
                                freq_str = f"({task.days_of_week})"
                            elif task.frequency in [Frequency.EVERYDAY, Frequency.WEEKLY, Frequency.MONTHLY]:
                                freq_str = f"({task.frequency.name})"
                            
                            task_lines.append(f"🔹 {task.title} {freq_str}".strip())
                        
                        task_list = "\n".join(task_lines)
                        backup_message = (
                            f"🔔 НАПОМИНАНИЕ: У вас есть невыполненные задачи на сегодня:\n{task_list}\n\n"
                            f"(Это резервное напоминание, так как основное напоминание могло не дойти)"
                        )
                        
                        await context.bot.send_message(chat_id=user.telegram_id, text=backup_message)
                        logger.info(f"Sent BACKUP reminder to user {user.telegram_id} for {len(incomplete_tasks_today)} tasks.")
                        
                        # Update last_notified timestamp
                        update_user_last_notified(db, user.id)
                    else:
                        # No tasks today, but still update the notification timestamp
                        update_user_last_notified(db, user.id)
                        logger.info(f"User {user.telegram_id} has no incomplete tasks today, updated notification timestamp in backup check.")

                except Exception as e:
                    logger.error(f"Error in backup reminder for user {user.telegram_id}: {e}")
            
        except Exception as e:
            logger.error(f"Error during backup reminder job execution: {e}")
        finally:
            db.close()
            logger.info("Backup reminder check finished.")

    async def done_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mark a task as completed using task ID: /done <task_id>"""
        if not context.args:
            await update.message.reply_text(
                "Пожалуйста, укажите ID задачи после /done.\n"
                "Например: /done 5\n"
                "Чтобы увидеть ID задач, используйте /list_task"
            )
            return
            
        try:
            task_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("ID задачи должен быть числом. Например: /done 5")
            return
            
        db = SessionLocal()
        try:
            # Get the user
            user = get_user_by_telegram_id(db, update.effective_user.id)
            if not user:
                await update.message.reply_text("Вы не зарегистрированы. Используйте /start.")
                return
                
            # Find the task and verify it belongs to the user
            task = db.query(Task).filter(Task.id == task_id, Task.user_id == user.id).first()
            
            if not task:
                await update.message.reply_text(f"Задача #{task_id} не найдена или не принадлежит вам.")
                return
                
            # Check if already completed today
            if is_task_completed_today(db, task):
                await update.message.reply_text(f"Задача #{task_id} уже отмечена как выполненная сегодня.")
                return
                
            # Mark as completed
            task = complete_task(db, task_id)
            await update.message.reply_text(f"✅ Задача #{task_id}: '{task.title}' отмечена как выполненная!")
            
        except Exception as e:
            logger.error(f"Ошибка при отметке задачи как выполненной: {e}")
            await update.message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте еще раз.")
        finally:
            db.close()

    async def reset_tasks_job(self, context: CallbackContext) -> None:
        """Reset recurring tasks at midnight."""
        logger.info("Running daily task reset job...")
        db = SessionLocal()
        try:
            reset_count = reset_recurring_tasks(db)
            logger.info(f"Reset {reset_count} recurring tasks to uncompleted status.")
        except Exception as e:
            logger.error(f"Error during task reset job: {e}")
        finally:
            db.close()
            logger.info("Task reset job finished.")

    async def delete_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Delete a task using task ID: /delete <task_id>"""
        if not context.args:
            await update.message.reply_text(
                "Пожалуйста, укажите ID задачи после /delete.\n"
                "Например: /delete 5\n"
                "Чтобы увидеть ID задач, используйте /list_task"
            )
            return
            
        try:
            task_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("ID задачи должен быть числом. Например: /delete 5")
            return
            
        db = SessionLocal()
        try:
            # Get the user
            user = get_user_by_telegram_id(db, update.effective_user.id)
            if not user:
                await update.message.reply_text("Вы не зарегистрированы. Используйте /start.")
                return
                
            # Find the task and verify it belongs to the user
            task = db.query(Task).filter(Task.id == task_id, Task.user_id == user.id).first()
            
            if not task:
                await update.message.reply_text(f"Задача #{task_id} не найдена или не принадлежит вам.")
                return
                
            # Remember task title before deletion
            task_title = task.title
                
            # Delete the task
            success = delete_task(db, task_id)
            
            if success:
                await update.message.reply_text(f"🗑️ Задача #{task_id}: '{task_title}' удалена!")
            else:
                await update.message.reply_text(f"⚠️ Не удалось удалить задачу #{task_id}.")
            
        except Exception as e:
            logger.error(f"Ошибка при удалении задачи: {e}")
            await update.message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте еще раз.")
        finally:
            db.close()

    async def yearly_cleanup_job(self, context: CallbackContext) -> None:
        """Run yearly cleanup to remove old task completion records.
        This is scheduled to run monthly but only performs cleanup 
        on January 1st to avoid excessive DB operations.
        """
        today = datetime.now(timezone('Asia/Yekaterinburg'))
        
        # Only do cleanup on January 1st
        if today.month == 1 and today.day == 1:
            logger.info("Running yearly task completion history cleanup...")
            db = SessionLocal()
            try:
                deleted_count = schedule_yearly_cleanup(db)
                logger.info(f"Deleted {deleted_count} old task completion records.")
                
                # Notify admin if configured
                env_vars = get_env_vars()
                admin_chat_id = env_vars.ADMIN_CHAT_ID
                if admin_chat_id:
                    await context.bot.send_message(
                        chat_id=admin_chat_id,
                        text=f"✅ Yearly cleanup complete: removed {deleted_count} old task completion records."
                    )
            except Exception as e:
                logger.error(f"Error during yearly cleanup: {e}")
            finally:
                db.close()
                logger.info("Yearly cleanup job finished.")
        else:
            logger.info(f"Monthly check for yearly cleanup - skipping (not January 1st)")

    async def schedule_task_reminders(self, application: Application):
        """Планирует напоминания для всех задач с reminder_time."""
        db = SessionLocal()
        try:
            users = get_all_users(db)
            for user in users:
                tasks = get_tasks_by_user(db, user.id)
                for task in tasks:
                    if task.reminder_time:
                        # Планируем напоминание для каждой задачи
                        application.job_queue.run_daily(
                            self._make_task_reminder_callback(user.telegram_id, task.id),
                            time=task.reminder_time
                        )
        finally:
            db.close()

    def _make_task_reminder_callback(self, telegram_id, task_id):
        async def callback(context: CallbackContext):
            db = SessionLocal()
            try:
                task = db.query(Task).filter(Task.id == task_id).first()
                if task and not task.completed:
                    logger.info(f"Sending reminder for task {task.id} ({task.title}) at {task.reminder_time}")
                    await context.bot.send_message(chat_id=telegram_id, text=f"⏰ Напоминание: задача '{task.title}' ждет выполнения!")
            finally:
                db.close()
        return callback

    def run(self):
        logger.info("Starting bot")
        self._bot.run_polling()

    async def text_message_handler(self, update: Update, context: CallbackContext) -> None:
        if context.user_data.get("awaiting_points"):
            points_text = update.message.text.strip()
            try:
                points = int(points_text)
                if points < 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Пожалуйста, введите неотрицательное целое число для баллов.")
                return
            frequency_enum = context.user_data.get("frequency_enum")
            task_name = context.user_data.get("task_name")
            days_of_week = context.user_data.get("days_of_week")
            reminder_time = context.user_data.get("reminder_time") if "reminder_time" in context.user_data else None
            db = SessionLocal()
            try:
                user = get_user_by_telegram_id(db, update.effective_user.id)
                if not user:
                    await update.message.reply_text("You are not registered. Use /start.")
                    return
                task = create_task(
                    db=db,
                    user_id=user.id,
                    title=task_name,
                    frequency=frequency_enum,
                    days_of_week=days_of_week,
                    reminder_time=reminder_time,
                    points=points
                )
                logger.info(f"Task created: id={task.id}, user_id={user.id}, telegram_id={user.telegram_id}, points={points}")
                await update.message.reply_text(f"✅ Задача '{task_name}' с баллами {points} добавлена!")
            finally:
                db.close()
            context.user_data.pop("task_name", None)
            context.user_data.pop("frequency_enum", None)
            context.user_data.pop("selected_days", None)
            context.user_data.pop("days_of_week", None)
            context.user_data.pop("awaiting_points", None)
            return
        if "frequency_enum" in context.user_data and "task_name" in context.user_data:
            time_text = update.message.text.strip()
            match = re.match(r"^(\d{1,2}):(\d{2})$", time_text)
            if match:
                hour, minute = int(match.group(1)), int(match.group(2))
                reminder_time = time(hour, minute)
                context.user_data["reminder_time"] = reminder_time
                await update.message.reply_text("Введите количество баллов за выполнение этой задачи (целое число):")
                context.user_data["awaiting_points"] = True
                return
            else:
                await update.message.reply_text("⏰ Enter time in HH:MM format (e.g., 09:30) or press 'Skip'.")
                return

    async def points_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        db = SessionLocal()
        try:
            user = get_user_by_telegram_id(db, update.effective_user.id)
            if not user:
                await update.message.reply_text("Вы не зарегистрированы. Используйте /start.")
                return
            await update.message.reply_text(f"Ваши баллы: {user.user_points}")
        finally:
            db.close()

if __name__ == "__main__":
    from config import get_env_vars
    env = get_env_vars()
    token = env.TELEGRAM_BOT_TOKEN
    db_url = env.DB_URL
    TgBotClient(token, db_url).run()