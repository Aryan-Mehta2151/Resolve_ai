"""add tenant-bound inbound phone voice calls

Revision ID: 9f8c23ad1e72
Revises: c51ac6e04b72
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9f8c23ad1e72"
down_revision: str | None = "c51ac6e04b72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("phone_number_normalized", sa.String(length=16)))
    op.execute(
        """
        UPDATE customers
        SET phone_number_normalized =
            CASE
                WHEN phone_number IS NULL THEN NULL
                WHEN length(regexp_replace(phone_number, '[^0-9]', '', 'g'))
                    BETWEEN 8 AND 15
                THEN '+' || regexp_replace(phone_number, '[^0-9]', '', 'g')
                ELSE NULL
            END
        """
    )
    op.create_index(
        "ix_customers_organization_phone_normalized",
        "customers",
        ["organization_id", "phone_number_normalized"],
        unique=False,
    )

    op.create_table(
        "voice_phone_numbers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("provider_number_id", sa.String(length=128), nullable=False),
        sa.Column("phone_number", sa.String(length=16), nullable=False),
        sa.Column("service_user_id", sa.Uuid(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_voice_phone_number_service_membership",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_voice_phone_numbers_tenant_id"
        ),
        sa.UniqueConstraint(
            "provider_number_id", name="uq_voice_phone_numbers_provider_id"
        ),
    )
    op.create_index(
        "ix_voice_phone_numbers_organization_active",
        "voice_phone_numbers",
        ["organization_id", "is_active"],
        unique=False,
    )

    op.alter_column("voice_calls", "token_hash", existing_type=sa.String(length=64), nullable=True)
    op.add_column(
        "voice_calls",
        sa.Column("channel", sa.String(length=16), server_default="browser", nullable=False),
    )
    op.add_column("voice_calls", sa.Column("phone_number_config_id", sa.Uuid(), nullable=True))
    op.add_column("voice_calls", sa.Column("candidate_customer_id", sa.Uuid(), nullable=True))
    op.add_column(
        "voice_calls", sa.Column("phone_consent_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "voice_calls", sa.Column("customer_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("voice_calls", sa.Column("last_verification_tool_id", sa.String(length=128)))
    op.add_column("voice_calls", sa.Column("last_verification_result", sa.JSON(), nullable=True))
    op.create_check_constraint(
        "ck_voice_call_channel", "voice_calls", "channel in ('browser', 'phone')"
    )
    op.create_foreign_key(
        "fk_voice_call_phone_number_same_organization",
        "voice_calls",
        "voice_phone_numbers",
        ["organization_id", "phone_number_config_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_voice_call_candidate_same_organization",
        "voice_calls",
        "customers",
        ["organization_id", "candidate_customer_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "voice_customer_verifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("call_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0 and attempt_count <= 5",
            name="ck_voice_verification_attempt_count",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["voice_calls.organization_id", "voice_calls.id"],
            name="fk_voice_verification_call_same_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_voice_verification_customer_same_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_voice_verifications_org_customer_created",
        "voice_customer_verifications",
        ["organization_id", "customer_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_voice_verifications_org_customer_created",
        table_name="voice_customer_verifications",
    )
    op.drop_table("voice_customer_verifications")
    op.drop_constraint(
        "fk_voice_call_candidate_same_organization", "voice_calls", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_voice_call_phone_number_same_organization", "voice_calls", type_="foreignkey"
    )
    op.drop_constraint("ck_voice_call_channel", "voice_calls", type_="check")
    op.drop_column("voice_calls", "customer_verified_at")
    op.drop_column("voice_calls", "phone_consent_at")
    op.drop_column("voice_calls", "last_verification_result")
    op.drop_column("voice_calls", "last_verification_tool_id")
    op.drop_column("voice_calls", "candidate_customer_id")
    op.drop_column("voice_calls", "phone_number_config_id")
    op.drop_column("voice_calls", "channel")
    op.alter_column("voice_calls", "token_hash", existing_type=sa.String(length=64), nullable=False)
    op.drop_index(
        "ix_voice_phone_numbers_organization_active", table_name="voice_phone_numbers"
    )
    op.drop_table("voice_phone_numbers")
    op.drop_index(
        "ix_customers_organization_phone_normalized", table_name="customers"
    )
    op.drop_column("customers", "phone_number_normalized")
