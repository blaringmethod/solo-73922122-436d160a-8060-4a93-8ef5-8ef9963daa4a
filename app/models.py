"""ORM models for the research FHIR store.

The production target is PostgreSQL (JSONB). A portable JSON variant is used so
the same models work under SQLite for local/offline self-tests; locking and
constraint semantics that matter for correctness are exercised on PostgreSQL.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# JSONB on PostgreSQL, plain JSON elsewhere (SQLite test profile).
JsonType = JSONB().with_variant(JSON(), "sqlite")


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    status: Mapped[str] = mapped_column(String(16), default="created")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    resource: Mapped[dict] = mapped_column(JsonType, nullable=False)

    identifiers: Mapped[list["PatientIdentifier"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan", lazy="selectin"
    )


class PatientIdentifier(Base):
    __tablename__ = "patient_identifiers"
    __table_args__ = (
        UniqueConstraint("system", "value", name="uq_patient_identifier"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # system is nullable in FHIR (identifier without system); use empty string
    # internally for the unique constraint instead of NULL.
    system: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    value: Mapped[str] = mapped_column(String(512), nullable=False)

    patient: Mapped[Patient] = relationship(back_populates="identifiers")


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="created")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    resource: Mapped[dict] = mapped_column(JsonType, nullable=False)


class DiagnosticReport(Base):
    __tablename__ = "diagnostic_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="created")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    resource: Mapped[dict] = mapped_column(JsonType, nullable=False)

    links: Mapped[list["ReportLink"]] = relationship(
        back_populates="report", cascade="all, delete-orphan", lazy="selectin"
    )


class ReportLink(Base):
    """Association table: DiagnosticReport.result -> Observation."""

    __tablename__ = "report_links"
    __table_args__ = (
        UniqueConstraint("report_id", "observation_id", name="uq_report_observation"),
        Index("ix_report_links_observation_id", "observation_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    report_id: Mapped[str] = mapped_column(
        ForeignKey("diagnostic_reports.id", ondelete="CASCADE"), nullable=False
    )
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("observations.id", ondelete="RESTRICT"), nullable=False
    )

    report: Mapped[DiagnosticReport] = relationship(back_populates="links")


class ReportResultBinding(Base):
    """Version-pinned binding of one DiagnosticReport version's result entry
    to the exact Observation version that was in force when the report version
    was committed.

    A row is written for every result reference of every report version. When
    a report version predates this binding feature (or the referenced
    observation cannot be traced to a version), ``traceable`` is False and
    ``observation_version`` is NULL; that untraceable state is deliberately
    carried forward by subsequent corrections until the observation itself is
    corrected in the same bundle.
    """

    __tablename__ = "report_result_bindings"
    __table_args__ = (
        UniqueConstraint(
            "report_id",
            "report_version",
            "position",
            name="uq_report_version_binding_position",
        ),
        Index("ix_report_bindings_lookup", "report_id", "report_version"),
        Index("ix_report_bindings_observation", "observation_id", "observation_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    report_id: Mapped[str] = mapped_column(
        ForeignKey("diagnostic_reports.id", ondelete="CASCADE"), nullable=False
    )
    report_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("observations.id", ondelete="RESTRICT"), nullable=False
    )
    # NULL when the binding is not traceable to an observation version.
    observation_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    traceable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResourceVersion(Base):
    """Append-only version history for every stored resource."""

    __tablename__ = "resource_versions"
    __table_args__ = (
        Index("ix_resource_versions_lookup", "resource_type", "resource_id", "version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    resource: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyRecord(Base):
    """Idempotency / exactly-once ledger for submitted Bundles."""

    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(16), default="pending")  # pending|completed|failed
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
