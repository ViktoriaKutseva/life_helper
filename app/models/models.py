from datetime import datetime

from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Time
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base
import sqlalchemy

from enums.frequency import Frequency  

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    last_notified = Column(DateTime, nullable=True)  # Track when the user was last notified
    user_points = Column(Integer, default=0)  # Общие баллы пользователя

    tasks = relationship("Task", back_populates="user")

class Task(Base):
    __tablename__ = "tasks"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    title = Column(String, nullable=False)
    completed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    frequency = Column(sqlalchemy.Enum(Frequency, native_enum=False), nullable=False)
    days_of_week = Column(String, nullable=True)
    last_completed = Column(DateTime, nullable=True)
    reminder_time = Column(Time, nullable=True)
    points = Column(Integer, default=0)  # Баллы за выполнение задачи

    user = relationship("User", back_populates="tasks")
    completions = relationship("TaskCompletion", back_populates="task", cascade="all, delete-orphan")

class TaskCompletion(Base):
    __tablename__ = "task_completions"
    
    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    completed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    task = relationship("Task", back_populates="completions")
    