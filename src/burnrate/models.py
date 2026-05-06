"""SQLAlchemy ORM models.

All costs stored in pence (integer). Never pounds, never dollars.
See docs/architecture.md for the full schema rationale.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    magic_link_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    api_keys: Mapped[list[ApiKey]] = relationship("ApiKey", back_populates="user", cascade="all, delete-orphan")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    br_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    anthropic_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[str] = mapped_column(String(50), nullable=False, default="hobby")
    daily_cap_pence: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    kill_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    alert_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship("User", back_populates="api_keys")
    usage_events: Mapped[list[UsageEvent]] = relationship("UsageEvent", back_populates="api_key", cascade="all, delete-orphan")
    triggers: Mapped[list[Trigger]] = relationship("Trigger", back_populates="api_key", cascade="all, delete-orphan")
    cap_today: Mapped[CapToday | None] = relationship("CapToday", back_populates="api_key", uselist=False)


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_key_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_pence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cap_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    api_key: Mapped[ApiKey] = relationship("ApiKey", back_populates="usage_events")


class Trigger(Base):
    """Trigger rules — scaffolded in W1, anomaly rule enabled in W4."""

    __tablename__ = "triggers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_key_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False)
    condition_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    actions_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    api_key: Mapped[ApiKey] = relationship("ApiKey", back_populates="triggers")


class CapToday(Base):
    """Denormalised daily spend total — fast cap check on hot path.

    One row per (api_key_id, date). Reset nightly by the worker cron.
    Uses INSERT ... ON CONFLICT DO UPDATE for atomic increment.
    """

    __tablename__ = "caps_today"

    api_key_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, primary_key=True)
    total_pence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    api_key: Mapped[ApiKey] = relationship("ApiKey", back_populates="cap_today")
