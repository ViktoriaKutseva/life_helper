from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, cast, Integer, func, extract, desc
from models.models import User, Task, TaskCompletion
from enums.frequency import Frequency
from datetime import datetime, timedelta, date
from typing import Optional, List


# 🟢 CREATE User
def create_user(db: Session, telegram_id: int):
    db_user = User(telegram_id=telegram_id)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

# 🟢 GET User by Telegram ID
def get_user_by_telegram_id(db: Session, telegram_id: int):
    return db.query(User).filter(User.telegram_id == telegram_id).first()

# 🟢 GET All Users
def get_all_users(db: Session):
    """Returns all registered users."""
    return db.query(User).all()

# 🟢 CREATE Task
def create_task(db: Session, user_id: int, title: str, frequency: Frequency, days_of_week: str | None = None):
    db_task = Task(user_id=user_id, title=title, frequency=frequency, days_of_week=days_of_week)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task

# 🟢 GET Tasks by User
def get_tasks_by_user(db: Session, user_id: int):
    return db.query(Task).filter(Task.user_id == user_id).all()

# 🟢 GET Tasks Due Today by User
def get_tasks_due_today(db: Session, user_id: int):
    """Returns a list of tasks for the user that are due today.
    Includes completion status for today.
    """
    today = datetime.utcnow() # Use UTC consistently
    today_weekday_abbr = today.strftime('%a').upper() # e.g., MON, TUE
    today_day_of_month = today.day # e.g., 15
    today_weekday_num = today.weekday() # Monday is 0 and Sunday is 6
    
    # For SQLite compatibility, use strftime instead of extract
    # SQLite strftime('%w',...) returns 0 for Sunday, 1-6 for Mon-Sat
    # Python weekday() returns 0 for Monday, 6 for Sunday
    # So we need to do: (weekday_num + 1) % 7 to compare with strftime('%w')
    
    adjusted_weekday = (today_weekday_num + 1) % 7

    # SQLAlchemy query with SQLite-compatible date functions
    tasks = db.query(Task).filter(
        Task.user_id == user_id,
        or_(
            Task.frequency == Frequency.EVERYDAY,
            and_(
                Task.frequency == Frequency.SPECIFIC_DAYS,
                Task.days_of_week.isnot(None),
                Task.days_of_week.contains(today_weekday_abbr)
            ),
            and_(
                Task.frequency == Frequency.WEEKLY,
                # SQLite-compatible weekday extraction
                cast(func.strftime('%w', Task.created_at), Integer) == adjusted_weekday
            ),
            and_(
                Task.frequency == Frequency.MONTHLY,
                # SQLite-compatible day of month extraction
                cast(func.strftime('%d', Task.created_at), Integer) == today_day_of_month
            )
        )
    ).all()
    
    # For each task, check if it was completed today
    for task in tasks:
        task.completed = is_task_completed_today(db, task)
    
    return tasks

# 🟢 UPDATE Task (Mark as Completed)
def complete_task(db: Session, task_id: int):
    """Mark a task as completed by creating a completion record"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        # Create a new completion record
        completion = TaskCompletion(
            task_id=task.id, 
            completed_at=datetime.utcnow()
        )
        db.add(completion)
        
        # Update the last_completed timestamp on the task itself
        task.last_completed = completion.completed_at
        
        db.commit()
        db.refresh(task)
    return task

def get_task_completion_for_day(db: Session, task_id: int, target_date: datetime = None):
    """Check if a task was completed on a specific day"""
    if target_date is None:
        target_date = datetime.utcnow()
    
    # Convert to date only for comparison
    target_day = target_date.date()
    
    # Find any completion records for this task on the target day
    completion = (db.query(TaskCompletion)
                  .filter(TaskCompletion.task_id == task_id)
                  .filter(func.date(TaskCompletion.completed_at) == target_day)
                  .order_by(desc(TaskCompletion.completed_at))
                  .first())
    
    return completion

def is_task_completed_today(db: Session, task: Task):
    """Check if a task is completed today"""
    return get_task_completion_for_day(db, task.id) is not None

def get_task_completions(db: Session, task_id: int, limit: int = 30):
    """Get the recent completion history for a task"""
    return (db.query(TaskCompletion)
            .filter(TaskCompletion.task_id == task_id)
            .order_by(desc(TaskCompletion.completed_at))
            .limit(limit)
            .all())

def delete_old_completions(db: Session, days_to_keep: int = 365):
    """Delete completion records older than the specified number of days"""
    cutoff_date = datetime.utcnow() - timedelta(days=days_to_keep)
    
    result = db.query(TaskCompletion).filter(
        TaskCompletion.completed_at < cutoff_date
    ).delete()
    
    db.commit()
    return result

# 🟢 Reset Recurring Tasks
def reset_recurring_tasks(db: Session):
    """Reset completed recurring tasks that should be active again.
    
    This function should be called once per day, preferably at midnight.
    It resets all completed recurring tasks to uncompleted status,
    except for ONCE tasks which remain completed.
    """
    # Get all completed tasks except ONCE
    completed_recurring_tasks = db.query(Task).filter(
        Task.completed == True,
        Task.frequency != Frequency.ONCE
    ).all()
    
    today = datetime.utcnow().date()
    reset_count = 0
    
    for task in completed_recurring_tasks:
        should_reset = False
        
        # If no last_completed date recorded, reset it
        if not task.last_completed:
            should_reset = True
        else:
            last_completed_date = task.last_completed.date()
            
            # Different logic based on frequency
            if task.frequency == Frequency.EVERYDAY:
                # Reset if it was completed on a previous day
                if last_completed_date < today:
                    should_reset = True
                    
            elif task.frequency == Frequency.WEEKLY:
                # Reset if it was completed 7+ days ago
                if (today - last_completed_date).days >= 7:
                    should_reset = True
                    
            elif task.frequency == Frequency.MONTHLY:
                # Reset if it was completed in a previous month
                if (last_completed_date.month != today.month or 
                    last_completed_date.year != today.year):
                    should_reset = True
                    
            elif task.frequency == Frequency.SPECIFIC_DAYS:
                # Reset if it was completed on a previous day and today is one of the specific days
                if last_completed_date < today:
                    # Check if today is one of the specific days
                    today_abbr = today.strftime('%a').upper()  # E.g., 'MON', 'TUE'
                    if task.days_of_week and today_abbr in task.days_of_week.split(','):
                        should_reset = True
        
        # Reset the task if needed
        if should_reset:
            task.completed = False
            reset_count += 1
    
    if reset_count > 0:
        db.commit()
    
    return reset_count

# 🟢 DELETE Task
def delete_task(db: Session, task_id: int):
    db_task = db.query(Task).filter(Task.id == task_id).first()
    if db_task:
        db.delete(db_task)
        db.commit()
    return db_task

# 🟢 UPDATE User Last Notified
def update_user_last_notified(db: Session, user_id: int):
    """Update the last_notified timestamp for a user."""
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.last_notified = datetime.utcnow()
        db.commit()
        db.refresh(user)
    return user

def schedule_yearly_cleanup(db: Session):
    """Schedule yearly cleanup - this should be called periodically"""
    # Keep completion history for a year
    return delete_old_completions(db, days_to_keep=365)

