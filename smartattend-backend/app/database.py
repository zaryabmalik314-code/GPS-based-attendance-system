import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Railway injects DATABASE_URL for Postgres. Falls back to local SQLite for dev.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./smartattend.db")

# Railway's Postgres URL starts with postgres:// or postgresql:// — force the
# pg8000 driver (pure Python, no native libpq dependency) instead of the
# default psycopg2, which has had native-library issues on Railway's builder.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+pg8000://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+pg8000://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_simple_migrations():
    """
    SQLAlchemy's Base.metadata.create_all() only creates tables that don't
    exist yet — it does NOT add new columns to tables that already exist.
    Since this project has no Alembic migration setup, this function checks
    for a few specific columns added after the tables were first created,
    and adds them via raw ALTER TABLE if missing. Safe to call on every
    startup — it's a no-op once the column already exists.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "faculty" not in inspector.get_table_names():
        return  # table doesn't exist yet — create_all will make it fresh, no migration needed

    existing_columns = {col["name"] for col in inspector.get_columns("faculty")}

    with engine.connect() as conn:
        if "profile_photo" not in existing_columns:
            conn.execute(text("ALTER TABLE faculty ADD COLUMN profile_photo TEXT"))
            conn.commit()
            print("[migration] Added missing column: faculty.profile_photo")
