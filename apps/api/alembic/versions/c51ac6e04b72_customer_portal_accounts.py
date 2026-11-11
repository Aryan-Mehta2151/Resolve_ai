"""add customer memberships and email verification tokens

Revision ID: c51ac6e04b72
Revises: fe3cf837d75a
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c51ac6e04b72"
down_revision: str | None = "fe3cf837d75a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("organization_memberships") as batch_op:
        batch_op.drop_constraint("ck_membership_role", type_="check")
        batch_op.drop_constraint("ck_membership_status", type_="check")
        batch_op.add_column(sa.Column("customer_id", sa.Uuid(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_membership_organization_customer", ["organization_id", "customer_id"]
        )
        batch_op.create_check_constraint(
            "ck_membership_role",
            "role in ('admin', 'supervisor', 'support_agent', 'viewer', 'customer')",
        )
        batch_op.create_check_constraint(
            "ck_membership_status", "status in ('active', 'pending', 'suspended')"
        )
        batch_op.create_check_constraint(
            "ck_membership_customer_profile",
            "(role = 'customer' and customer_id is not null) "
            "or (role != 'customer' and customer_id is null)",
        )
        batch_op.create_foreign_key(
            "fk_membership_customer_same_organization",
            "customers",
            ["organization_id", "customer_id"],
            ["organization_id", "id"],
            ondelete="CASCADE",
        )

    op.create_table(
        "customer_email_verifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_customer_verification_membership_same_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_customer_verification_profile_same_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_customer_email_verifications_token_hash"),
    )
    op.create_index(
        "ix_customer_email_verifications_org_user_created",
        "customer_email_verifications",
        ["organization_id", "user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_email_verifications_org_user_created",
        table_name="customer_email_verifications",
    )
    op.drop_table("customer_email_verifications")
    with op.batch_alter_table("organization_memberships") as batch_op:
        batch_op.drop_constraint("fk_membership_customer_same_organization", type_="foreignkey")
        batch_op.drop_constraint("ck_membership_customer_profile", type_="check")
        batch_op.drop_constraint("ck_membership_role", type_="check")
        batch_op.drop_constraint("ck_membership_status", type_="check")
        batch_op.drop_constraint("uq_membership_organization_customer", type_="unique")
        batch_op.drop_column("customer_id")
        batch_op.create_check_constraint(
            "ck_membership_role", "role in ('admin', 'supervisor', 'support_agent', 'viewer')"
        )
        batch_op.create_check_constraint(
            "ck_membership_status", "status in ('active', 'suspended')"
        )
