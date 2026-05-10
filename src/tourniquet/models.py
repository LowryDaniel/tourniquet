"""SQLAlchemy ORM models.

All costs stored in USD cents (integer). Canonical currency is USD.
Display formatting is handled by tourniquet.billing.formatting.format_money().
See docs/architecture.md for the full schema rationale.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import CHAR, TypeDecorator


class _UUIDType(TypeDecorator):
    """UUID column portable across Postgres and SQLite.

    Stored as CHAR(36) string in SQLite, native UUID in Postgres.
    """
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID as PG_UUID
            return dialect.type_descriptor(PG_UUID())
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)


UUID = _UUIDType
JSONB = JSON


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    magic_link_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    api_keys: Mapped[list[ApiKey]] = relationship(
        "ApiKey", back_populates="user", cascade="all, delete-orphan"
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    tq_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 hex of the raw tq_* token. Indexed unique so the proxy auth
    # path can do a single lookup instead of a bcrypt linear scan. Nullable
    # to support legacy rows created before C3; backfilled on first match
    # via the bcrypt fallback path.
    tq_token_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True,
    )
    anthropic_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[str] = mapped_column(String(50), nullable=False, default="standard")
    daily_cap_usd_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    kill_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    alert_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auto_tune_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="off")
    # Values: "off" | "suggest" | "creep"
    absolute_ceiling_usd_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=10000)
    # 10000 cents = $100/day default ceiling. Auto-tune creep can never exceed this.
    lifted_cap_usd_cents: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    lift_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship("User", back_populates="api_keys")
    usage_events: Mapped[list[UsageEvent]] = relationship(
        "UsageEvent", back_populates="api_key", cascade="all, delete-orphan"
    )
    triggers: Mapped[list[Trigger]] = relationship(
        "Trigger", back_populates="api_key", cascade="all, delete-orphan"
    )
    cap_today: Mapped[CapToday | None] = relationship(
        "CapToday", back_populates="api_key", uselist=False
    )


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=uuid.uuid4)
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cap_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    api_key: Mapped[ApiKey] = relationship("ApiKey", back_populates="usage_events")


class Trigger(Base):
    """Trigger rules — scaffolded in W1, anomaly rule enabled in W4."""

    __tablename__ = "triggers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=uuid.uuid4)
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False
    )
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

    api_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, primary_key=True)
    total_usd_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    api_key: Mapped[ApiKey] = relationship("ApiKey", back_populates="cap_today")


class ApiKeyAction(Base):
    """Audit log of cap-changing actions per key.

    Every kill / lift / bump / manual cap-change writes one row here. Powers
    the per-key "Action history" tab on the dashboard so operators can see
    what happened, when, and from which channel — even when the resulting
    cap value didn't visibly change (e.g. killing a key that was already
    at minimum).

    Field semantics:
      action:   kill_now | lift_by_amount | lift_mode | cap_set |
                recovery_offered | alert_fired
      source:   slack_socket | telegram_poll | web | cli | proxy | auto
      summary:  one-line human-readable description shown in the UI
      details:  optional structured payload (cap_before, cap_after, mode,
                amount_cents, today_spend_cents, etc.) for diagnostics
    """

    __tablename__ = "api_key_actions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=uuid.uuid4)
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    api_key: Mapped[ApiKey] = relationship("ApiKey")
