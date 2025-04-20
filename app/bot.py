from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, ContextTypes, CallbackQueryHandler, JobQueue
from sqlalchemy.orm import sessionmaker
from loguru import logger
from datetime import datetime, time, timedelta
from functools import partial
from pytz import timezone
import asyncio
import sqlalchemy
from sqlalchemy import exc  # Add explicit import for sqlalchemy.exc

from database.database import engine, SessionLocal
from services.crud import (
    create_user, get_user_by_telegram_id, create_task, get_tasks_by_user,
    get_all_users, get_tasks_due_today, complete_task, reset_recurring_tasks,
    update_user_last_notified
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


    async def post_init(self, application: Application) -> None:
        await application.bot.set_my_commands([
            BotCommand("start", "Начало работы с ботом"),
            BotCommand("add_task", "Добавить задачу"),
            BotCommand("list_task", "Показать все задачи"),
            BotCommand("today_tasks", "Задачи на сегодня"),
            BotCommand("done", "Отметить задачу как выполненную: /done <ID задачи>")
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
        
        # Set the time to specified Almaty time 
        target_time = time(17, 11, 0, tzinfo=timezone('Asia/Almaty'))
        job_queue.run_daily(self.send_daily_reminders, target_time)
        logger.info(f"Scheduled daily reminders for {target_time} (Almaty time)")
        
        # Schedule daily reset at midnight
        midnight = time(0, 0, 0, tzinfo=timezone('Asia/Almaty'))
        job_queue.run_daily(self.reset_tasks_job, midnight)
        logger.info(f"Scheduled daily task reset for {midnight} (Almaty time)")
        
        # Schedule backup notifications every 6 hours
        job_queue.run_repeating(
            self.send_backup_reminders, 
            interval=timedelta(hours=6), 
            first=timedelta(hours=6)
        )
        logger.info("Scheduled backup reminders every 6 hours")

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
                    
                # Check if already completed
                if task.completed:
                    await query.edit_message_text("Эта задача уже отмечена как выполненная. Обновите список командой /list_task")
                    db.close()
                    return
                    
                # Mark as completed
                task = complete_task(db, task_id)
                
                # Update the message to reflect the change
                user_tasks = get_tasks_by_user(db, user.id)
                new_message, new_markup = await self._format_task_list(user_tasks, "📌 Ваши задачи:", with_buttons=True)
                
                await query.edit_message_text(new_message, reply_markup=new_markup)
                
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
                    await query.edit_message_text("Произошла ошибка: неверная частота.")
                    return # Exit early

                if frequency_enum == Frequency.SPECIFIC_DAYS:
                    # Show day selection keyboard
                    keyboard = self._build_day_selection_keyboard(selected_days)
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(
                        f"Задача: {task_name}\nВыберите дни недели (или снимите выбор). Нажмите Done когда закончите:",
                        reply_markup=reply_markup
                    )
                else:
                    # Save task with selected frequency (ONCE, EVERYDAY, WEEKLY, MONTHLY)
                    create_task(
                        db=db,
                        user_id=user.id,
                        title=task_name,
                        frequency=frequency_enum
                        # days_of_week is omitted, defaults to None in DB
                    )
                    await query.edit_message_text(f"✅ Задача '{task_name}' с частотой '{frequency_str}' добавлена!")
                    # Clean up user_data
                    context.user_data.pop("task_name", None)
                    context.user_data.pop("selected_days", None)

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

            elif callback_data == "day_done":
                if not selected_days:
                    await query.answer("Вы не выбрали ни одного дня!", show_alert=True)
                    # Don't close the message here, let the user select days or cancel implicitly
                    return # Exit without saving

                # Sort days according to DAYS_OF_WEEK order before joining
                days_str = ",".join(sorted(list(selected_days), key=DAYS_OF_WEEK.index))
                create_task(
                    db=db,
                    user_id=user.id,
                    title=task_name,
                    frequency=Frequency.SPECIFIC_DAYS,
                    days_of_week=days_str
                )
                await query.edit_message_text(f"✅ Задача '{task_name}' добавлена для дней: {days_str}!")
                # Clean up user_data
                context.user_data.pop("task_name", None)
                context.user_data.pop("selected_days", None)

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
            # Task ID will help identify which task to mark as completed
            task_id = task.id
            
            freq_str = task.frequency.name
            if task.frequency == Frequency.SPECIFIC_DAYS and task.days_of_week:
                freq_str = f"Specific ({task.days_of_week})"
            elif task.frequency == Frequency.SPECIFIC_DAYS:
                freq_str = "Specific (Дни не указаны?)"
                logger.warning(f"Task {task.id} has SPECIFIC_DAYS frequency but no days_of_week set.")

            status = '✅' if task.completed else '❌'
            
            # Only show completion button for incomplete tasks
            if not task.completed and with_buttons:
                button = InlineKeyboardButton(f"✅ #{task_id}", callback_data=f"complete_{task_id}")
                buttons.append([button])
            
            task_lines.append(f"🔹 #{task_id}: {task.title} ({freq_str}) {status}")
        
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
            message_text, markup = await self._format_task_list(user_tasks, "📌 Ваши задачи:", with_buttons=True)
            
            # Send with buttons if there are any incomplete tasks
            await update.message.reply_text(message_text, reply_markup=markup)

        except Exception as e:
            logger.error(f"Ошибка при получении списка задач: {e}")
            await update.message.reply_text("⚠️ Ошибка при получении списка задач.")
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
        today = datetime.now(timezone('Asia/Almaty')).date()
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
                        # Convert to Almaty timezone for comparison
                        last_notified_almaty = user.last_notified.astimezone(timezone('Asia/Almaty'))
                        if last_notified_almaty.date() == today:
                            # User already notified today, skip
                            logger.info(f"User {user.telegram_id} already notified today at {last_notified_almaty}, skipping backup.")
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
                
            # Check if already completed
            if task.completed:
                await update.message.reply_text(f"Задача #{task_id} уже отмечена как выполненная.")
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

    def run(self):
        logger.info("Starting bot")
        self._bot.run_polling()