from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Plan(Base):
    __tablename__ = "plan"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    requests_per_minute: Mapped[int] = mapped_column(Integer)
    max_concurrency: Mapped[int] = mapped_column(Integer)


class PlanModel(Base):
    __tablename__ = "plan_model"

    plan_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("plan.code", ondelete="CASCADE"), primary_key=True
    )
    model_name: Mapped[str] = mapped_column(String(128), primary_key=True)


class Organization(Base):
    __tablename__ = "organization"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    plan_code: Mapped[str] = mapped_column(String(32), ForeignKey("plan.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    plan: Mapped[Plan] = relationship(lazy="selectin")


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organization.id"))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organization: Mapped[Organization] = relationship(lazy="selectin")


class ApiKey(Base):
    __tablename__ = "api_key"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("app_user.id"))
    key_id: Mapped[str] = mapped_column(String(64), unique=True)
    secret_hash: Mapped[str] = mapped_column(String(256))
    prefix: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[AppUser] = relationship(lazy="selectin")


class Request(Base):
    __tablename__ = "request"
    __table_args__ = (Index("ix_request_api_key_id_created_at", "api_key_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    api_key_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("api_key.id"))
    model_name: Mapped[str] = mapped_column(String(128))
    endpoint: Mapped[str] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    completion_tokens: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    status_code: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
