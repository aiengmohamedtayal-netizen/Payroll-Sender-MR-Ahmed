"""
Database Configuration and Engine Management for Neon PostgreSQL.
- Loads connection string securely from environment (DATABASE_URL or .env/.env.local).
- Uses SQLAlchemy with psycopg3 driver.
- Configures connection pooling and pre-ping to handle Neon scale-to-zero.
- Never logs connection credentials or secrets.
"""
import os
import sys
import logging
from typing import Generator, Tuple
from contextlib import contextmanager

from dotenv import load_dotenv
import sqlalchemy
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from sqlalchemy.exc import SQLAlchemyError, OperationalError

# Configure logger
logger = logging.getLogger("payroll.database")

# Load environment variables (.env.local or .env)
# Priority: Existing os.environ -> .env.local -> .env
DATA_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
env_local = os.path.join(DATA_DIR, ".env.local")
env_file = os.path.join(DATA_DIR, ".env")

if os.path.exists(env_local):
    load_dotenv(env_local)
elif os.path.exists(env_file):
    load_dotenv(env_file)

# Fetch DATABASE_URL
RAW_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

def get_normalized_database_url(url: str = RAW_DATABASE_URL) -> str:
    """Normalize connection URL for SQLAlchemy + psycopg (psycopg3)."""
    if not url:
        return ""
    # Strip enclosing quotes if present
    if (url.startswith('"') and url.endswith('"')) or (url.startswith("'") and url.endswith("'")):
        url = url[1:-1]
    
    # Convert postgresql:// to postgresql+psycopg://
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    elif url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    
    return url

DATABASE_URL = get_normalized_database_url()

if not DATABASE_URL:
    logger.warning("DATABASE_URL is not configured in environment or .env/.env.local")

# Declarative Base
Base = declarative_base()

# SQLAlchemy Engine & Session Configuration
# Neon best practices:
# - pool_pre_ping=True: Tests connections before using, automatically handles scale-to-zero wake-up
# - pool_recycle=1800: Recycles connections every 30 mins to avoid stale connections
# - pool_size=10, max_overflow=20: Handles concurrency smoothly
try:
    if DATABASE_URL:
        engine = create_engine(
            DATABASE_URL,
            pool_size=10,
            max_overflow=20,
            pool_recycle=1800,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 15} if "sqlite" not in DATABASE_URL else {}
        )
    else:
        # Placeholder or dummy engine if unconfigured
        engine = None
except Exception as e:
    logger.error("Failed to initialize database engine: %s", str(e))
    engine = None

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) if engine else None


class DatabaseError(Exception):
    """Base exception for database failures."""
    pass


class DatabaseConnectionError(DatabaseError):
    """Raised when Neon PostgreSQL is unreachable."""
    pass


def check_db_connection() -> Tuple[bool, str]:
    """Health check for Neon PostgreSQL connection."""
    if not engine:
        return False, "Database engine is not initialized (DATABASE_URL missing)."
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1;"))
            return True, "Connected to Neon PostgreSQL successfully."
    except OperationalError as e:
        logger.error("Database connection check failed: %s", str(e))
        return False, f"Could not connect to database server. Check network or credentials."
    except Exception as e:
        logger.error("Database check unexpected error: %s", str(e))
        return False, "Database verification error."


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Context manager for SQLAlchemy session lifecycle."""
    if not SessionLocal:
        raise DatabaseConnectionError("قاعدة البيانات غير مهيأة أو عنوان DATABASE_URL غير موجود.")
    
    db: Session = SessionLocal()
    try:
        yield db
    except OperationalError as e:
        db.rollback()
        logger.error("Database operational error: %s", str(e))
        raise DatabaseConnectionError("تعذر الاتصال بقاعدة البيانات السحابية (Neon).") from e
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    """Create tables if they do not exist."""
    if not engine:
        raise DatabaseConnectionError("Cannot initialize database: Engine is None.")
    import models  # import models to register with Base
    Base.metadata.create_all(bind=engine)
