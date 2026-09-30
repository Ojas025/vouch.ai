from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
import os

POSTGRES_URL = os.environ["POSTGRES_URL"]

# Long-lived db connectivity, owns the connection pool
engine = create_async_engine(
    POSTGRES_URL,
    echo=False,
    pool_size=10,
    max_overflow=20 # temp extra connections during traffic spikes
)

# factory for creating short-lived AsyncSession objects
# AsyncSession -> one unit of db work
# usage -> async with SessionLocal() as session:
SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)
