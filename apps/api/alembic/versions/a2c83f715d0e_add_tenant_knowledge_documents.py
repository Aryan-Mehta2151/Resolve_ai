"""add tenant knowledge documents and versions

Revision ID: a2c83f715d0e
Revises: d4e9f5b3a1c7
Create Date: 2026-10-25 23:30:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a2c83f715d0e"
down_revision: str | None = "d4e9f5b3a1c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_stores",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_store_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('pending', 'ready', 'failed')",
            name="ck_knowledge_stores_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", name="uq_knowledge_stores_organization"),
        sa.UniqueConstraint("provider_store_id"),
    )

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('active', 'deleting', 'deleted')",
            name="ck_knowledge_documents_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_knowledge_documents_creator_same_organization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_knowledge_documents_organization_id",
        ),
    )
    op.create_index(
        "ix_knowledge_documents_organization_status_created",
        "knowledge_documents",
        ["organization_id", "status", "created_at"],
        unique=False,
    )

    op.create_table(
        "knowledge_document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("provider_file_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version_number >= 1", name="ck_knowledge_document_versions_number"),
        sa.CheckConstraint("byte_size > 0", name="ck_knowledge_document_versions_byte_size"),
        sa.CheckConstraint(
            "status in ('queued', 'processing', 'ready', 'failed', 'superseded', 'deleted')",
            name="ck_knowledge_document_versions_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["knowledge_documents.organization_id", "knowledge_documents.id"],
            name="fk_knowledge_document_versions_document_same_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_knowledge_document_versions_organization_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "document_id",
            "version_number",
            name="uq_knowledge_document_versions_number",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "provider_file_id",
            name="uq_knowledge_document_versions_provider_file",
        ),
    )
    op.create_index(
        "ix_knowledge_document_versions_document_status",
        "knowledge_document_versions",
        ["organization_id", "document_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_document_versions_document_status",
        table_name="knowledge_document_versions",
    )
    op.drop_table("knowledge_document_versions")
    op.drop_index(
        "ix_knowledge_documents_organization_status_created",
        table_name="knowledge_documents",
    )
    op.drop_table("knowledge_documents")
    op.drop_table("knowledge_stores")
