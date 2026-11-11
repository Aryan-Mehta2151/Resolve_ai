"""add sequence numbers to conversation messages

Revision ID: 7e32bcc02a57
Revises: 3ffa7cd338e2
Create Date: 2026-10-25 22:19:49.990426

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7e32bcc02a57"
down_revision: str | None = "3ffa7cd338e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("sequence_number", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            WITH ranked_messages AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY organization_id, conversation_id
                        ORDER BY created_at, id
                    ) AS sequence_number
                FROM conversation_messages
            )
            UPDATE conversation_messages AS messages
            SET sequence_number = ranked_messages.sequence_number
            FROM ranked_messages
            WHERE messages.id = ranked_messages.id
            """
        )
    )
    op.alter_column(
        "conversation_messages",
        "sequence_number",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_conversation_messages_sequence",
        "conversation_messages",
        ["organization_id", "conversation_id", "sequence_number"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_conversation_messages_sequence",
        "conversation_messages",
        type_="unique",
    )
    op.drop_column("conversation_messages", "sequence_number")
