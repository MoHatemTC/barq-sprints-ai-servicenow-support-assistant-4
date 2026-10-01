"""
S3.3 — Persistent SQL state store mapping KB article sys_id -> SHA-256 body
hash + metadata, so the sync worker can tell new/modified/metadata-only/
unchanged apart without re-fetching or re-embedding unnecessarily.
"""
import datetime as dt

from sqlalchemy import create_engine, Column, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

Base = declarative_base()

engine = create_engine(
    settings.kb_state_db_url,
    connect_args={"check_same_thread": False} if settings.kb_state_db_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class ArticleState(Base):
    __tablename__ = "kb_article_state"

    sys_id = Column(String, primary_key=True)
    body_hash = Column(String, nullable=False)
    short_description = Column(String, nullable=True)
    kb_category = Column(String, nullable=True)
    workflow_state = Column(String, nullable=True)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_article_state(sys_id: str) -> ArticleState | None:
    with SessionLocal() as session:
        return session.get(ArticleState, sys_id)

def _flatten(value) -> str | None:
    """ServiceNow reference fields can come back as {'display_value': ..., 'link': ...}
    instead of a plain string, when sysparm_display_value=true is used. Extract just
    the display value so we can store it in a plain SQL column."""
    if isinstance(value, dict):
        return value.get("display_value")
    return value


def upsert_article_state(sys_id: str, body_hash: str, metadata: dict) -> None:
    with SessionLocal() as session:
        record = session.get(ArticleState, sys_id)
        short_description = _flatten(metadata.get("short_description"))
        kb_category = _flatten(metadata.get("kb_category"))
        workflow_state = _flatten(metadata.get("workflow_state"))

        if record is None:
            record = ArticleState(
                sys_id=sys_id,
                body_hash=body_hash,
                short_description=short_description,
                kb_category=kb_category,
                workflow_state=workflow_state,
            )
            session.add(record)
        else:
            record.body_hash = body_hash
            record.short_description = short_description
            record.kb_category = kb_category
            record.workflow_state = workflow_state
        session.commit()

def delete_article_state(sys_id: str) -> None:
    with SessionLocal() as session:
        record = session.get(ArticleState, sys_id)
        if record is not None:
            session.delete(record)
            session.commit()