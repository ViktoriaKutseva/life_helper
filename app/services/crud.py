from sqlalchemy.orm import Session
from models.models import User, Task
from enums.frequency import Frequency


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

# 🟢 CREATE Task
def create_task(db: Session, user_id: int, title: str, frequency: Frequency):
    db_task = Task(user_id=user_id, title=title, frequency=frequency)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task

# 🟢 GET Tasks by User
def get_tasks_by_user(db: Session, user_id: int):
    return db.query(Task).filter(Task.user_id == user_id).all()

# 🟢 UPDATE Task (Mark as Completed)
def complete_task(db: Session, task_id: int):
    db_task = db.query(Task).filter(Task.id == task_id).first()
    if db_task:
        db_task.completed = True
        db.commit()
        db.refresh(db_task)
    return db_task

# 🟢 DELETE Task
def delete_task(db: Session, task_id: int):
    db_task = db.query(Task).filter(Task.id == task_id).first()
    if db_task:
        db.delete(db_task)
        db.commit()
    return db_task

