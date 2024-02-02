import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from disco.config import SQLALCHEMY_DATABASE_URL

log = logging.getLogger(__name__)


engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    global Session
    with Session() as dbsession:
        with dbsession.begin():
            log.info("Yield dbsession")
            yield dbsession
