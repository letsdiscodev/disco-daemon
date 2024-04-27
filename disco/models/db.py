import logging

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from disco.config import SQLALCHEMY_ASYNC_DATABASE_URL, SQLALCHEMY_DATABASE_URL

log = logging.getLogger(__name__)


engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)


async_engine = create_async_engine(
    SQLALCHEMY_ASYNC_DATABASE_URL, connect_args={"check_same_thread": False}
)
AsyncSession = async_sessionmaker(autocommit=False, autoflush=False, bind=async_engine)
