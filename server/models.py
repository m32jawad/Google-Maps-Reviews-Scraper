"""The shared tables: a scrape run (Apify's "actor run" equivalent) and the
items a still-running run has produced so far."""
import secrets
from datetime import datetime, timezone

from sqlalchemy import (JSON, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# Lifecycle: QUEUED -> RUNNING -> SUCCEEDED | FAILED | TIMED_OUT | ABORTED
# ABORTING is a transient state between an abort request and the scheduler
# actually killing the container.
STATUSES = ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "TIMED_OUT",
            "ABORTING", "ABORTED")
FINISHED = ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED")

ACTORS = ("reviews", "places")


def new_run_id():
    return secrets.token_hex(8)


def utcnow():
    return datetime.now(timezone.utc)


class Run(Base):
    __tablename__ = "run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_run_id)
    actor: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED", index=True)

    input: Mapped[dict] = mapped_column(JSON)
    memory_mb: Mapped[int] = mapped_column(Integer)
    timeout_secs: Mapped[int] = mapped_column(Integer)

    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # docker:<container id> or proc:<pid>, owned by the executor
    handle: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list["RunItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True)

    def to_dict(self, include_result=False):
        d = {
            "id": self.id,
            "actor": self.actor,
            "status": self.status,
            "input": self.input,
            "memory_mb": self.memory_mb,
            "timeout_secs": self.timeout_secs,
            "progress": self.progress or {},
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
        if include_result:
            d["result"] = self.result
        return d


class RunItem(Base):
    """One item a worker has already found, written mid-run.

    Only exists so consumers can read reviews while the run is still RUNNING;
    the finished run's `result` stays the authoritative, complete set, so these
    rows are best-effort and may lag the true count by a page.
    """

    __tablename__ = "run_item"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_item_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("run.id", ondelete="CASCADE"), index=True)
    # Position of the item within the run, so pagination is stable and the
    # worker can append without ever reading back what it already wrote.
    seq: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)

    run: Mapped["Run"] = relationship(back_populates="items")
