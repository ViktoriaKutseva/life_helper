from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from config import get_env_vars 
from models.models import Base

# Load environment variables
env = get_env_vars()

# Database Engine
engine = create_engine(env.DB_URL, connect_args={"check_same_thread": False} if "sqlite" in env.DB_URL else {})

# Session
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
# Base = declarative_base()

# Function to create tables

Base.metadata.create_all(bind=engine)

