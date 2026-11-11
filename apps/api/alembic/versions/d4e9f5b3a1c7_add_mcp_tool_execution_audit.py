"""add MCP tool execution audit and workflow requester

Revision ID: d4e9f5b3a1c7
Revises: 7e32bcc02a57
Create Date: 2026-10-25 22:44:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e9f5b3a1c7"
down_revision: str | None = "7e32bcc02a57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_workflows",
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE agent_workflows AS workflows
            SET requested_by_user_id = conversations.created_by_user_id
            FROM conversations
            WHERE workflows.conversation_id = conversations.id
            """
        )
    )
    op.alter_column(
        "agent_workflows",
        "requested_by_user_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_workflows_requester_same_organization",
        "agent_workflows",
        "organization_memberships",
        ["organization_id", "requested_by_user_id"],
        ["organization_id", "user_id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "tool_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "operation in ('read', 'write')",
            name="ck_tool_executions_operation",
        ),
        sa.CheckConstraint(
            "status in ('succeeded', 'failed')",
            name="ck_tool_executions_status",
        ),
        sa.CheckConstraint(
            "duration_ms >= 0",
            name="ck_tool_executions_duration_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workflow_id"],
            ["agent_workflows.organization_id", "agent_workflows.id"],
            name="fk_tool_executions_workflow_same_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_tool_executions_actor_same_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_tool_executions_idempotency_key",
        ),
    )
    op.create_index(
        "ix_tool_executions_organization_workflow_created",
        "tool_executions",
        ["organization_id", "workflow_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_executions_organization_workflow_created",
        table_name="tool_executions",
    )
    op.drop_table("tool_executions")
    op.drop_constraint(
        "fk_workflows_requester_same_organization",
        "agent_workflows",
        type_="foreignkey",
    )
    op.drop_column("agent_workflows", "requested_by_user_id")
