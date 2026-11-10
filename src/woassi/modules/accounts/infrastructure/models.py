"""SQLAlchemy models of the accounts module."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from woassi.shared.infrastructure.db.base import Base


class AccountModel(Base):
    """Row of `accounts`. The phone number is unique and stored in E.164."""

    __tablename__ = "accounts"
    __table_args__ = (CheckConstraint("status IN ('ACTIVE', 'SUSPENDED')", name="status_valid"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(16), unique=True)
    email: Mapped[str | None] = mapped_column(String(254))
    password_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20))
    phone_verified: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshTokenModel(Base):
    """Row of `refresh_tokens`. Only the SHA-256 of the token is stored."""

    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VerificationCodeModel(Base):
    """Row of `verification_codes`. The code is stored hashed with the password hasher."""

    __tablename__ = "verification_codes"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('PHONE_VERIFICATION', 'PASSWORD_RESET')", name="purpose_valid"
        ),
        Index("ix_verification_codes_account_id_purpose", "account_id", "purpose"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(30))
    code_hash: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
