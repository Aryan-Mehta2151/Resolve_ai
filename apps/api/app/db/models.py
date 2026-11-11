from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MembershipRole(StrEnum):
    ADMIN = "admin"
    SUPERVISOR = "supervisor"
    SUPPORT_AGENT = "support_agent"
    VIEWER = "viewer"
    CUSTOMER = "customer"


class MembershipStatus(StrEnum):
    ACTIVE = "active"
    PENDING = "pending"
    SUSPENDED = "suspended"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    memberships: Mapped[list["OrganizationMembership"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    memberships: Mapped[list["OrganizationMembership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_membership_organization_user"),
        UniqueConstraint(
            "organization_id",
            "customer_id",
            name="uq_membership_organization_customer",
        ),
        CheckConstraint(
            "role in ('admin', 'supervisor', 'support_agent', 'viewer', 'customer')",
            name="ck_membership_role",
        ),
        CheckConstraint(
            "status in ('active', 'pending', 'suspended')",
            name="ck_membership_status",
        ),
        CheckConstraint(
            "(role = 'customer' and customer_id is not null) "
            "or (role != 'customer' and customer_id is null)",
            name="ck_membership_customer_profile",
        ),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_membership_customer_same_organization",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MembershipStatus.ACTIVE.value, server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class AuthRefreshSession(Base):
    __tablename__ = "auth_refresh_sessions"
    __table_args__ = (
        CheckConstraint(
            "revoked_at is null or revoked_at >= created_at", name="ck_refresh_revoked_at"
        ),
    )

    jti: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELAYED = "delayed"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class ShipmentStatus(StrEnum):
    LABEL_CREATED = "label_created"
    IN_TRANSIT = "in_transit"
    DELAYED = "delayed"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    EXCEPTION = "exception"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


class RefundStatus(StrEnum):
    PENDING = "pending"
    REQUIRES_APPROVAL = "requires_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SupportTicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_ON_CUSTOMER = "waiting_on_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"


class SupportTicketPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_customers_organization_id"),
        UniqueConstraint("organization_id", "email", name="uq_customers_organization_email"),
        Index("ix_customers_organization_name", "organization_id", "full_name"),
        Index(
            "ix_customers_organization_phone_normalized",
            "organization_id",
            "phone_number_normalized",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone_number: Mapped[str | None] = mapped_column(String(32))
    phone_number_normalized: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CustomerEmailVerification(Base):
    __tablename__ = "customer_email_verifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_customer_verification_membership_same_organization",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_customer_verification_profile_same_organization",
            ondelete="CASCADE",
        ),
        UniqueConstraint("token_hash", name="uq_customer_email_verifications_token_hash"),
        Index(
            "ix_customer_email_verifications_org_user_created",
            "organization_id",
            "user_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_products_organization_id"),
        UniqueConstraint("organization_id", "sku", name="uq_products_organization_sku"),
        CheckConstraint("unit_price >= 0", name="ck_products_price_nonnegative"),
        CheckConstraint("stock_quantity >= 0", name="ck_products_stock_nonnegative"),
        Index("ix_products_organization_name", "organization_id", "name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    stock_quantity: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_orders_organization_id"),
        UniqueConstraint("organization_id", "order_number", name="uq_orders_organization_number"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_orders_customer_same_organization",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status in ('pending', 'processing', 'shipped', 'delayed', 'delivered', 'cancelled')",
            name="ck_orders_status",
        ),
        CheckConstraint("total_amount >= 0", name="ck_orders_total_nonnegative"),
        CheckConstraint(
            "currency = upper(currency) and length(currency) = 3",
            name="ck_orders_currency",
        ),
        Index("ix_orders_organization_created", "organization_id", "created_at"),
        Index("ix_orders_organization_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    order_number: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=OrderStatus.PENDING.value
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            name="fk_order_items_order_same_organization",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "product_id"],
            ["products.organization_id", "products.id"],
            name="fk_order_items_product_same_organization",
            ondelete="RESTRICT",
        ),
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_order_items_price_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    product_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)


class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (
        UniqueConstraint("organization_id", "tracking_number", name="uq_shipments_tracking_number"),
        ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            name="fk_shipments_order_same_organization",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status in ('label_created', 'in_transit', 'delayed', "
            "'out_for_delivery', 'delivered', 'exception')",
            name="ck_shipments_status",
        ),
        Index("ix_shipments_organization_order", "organization_id", "order_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tracking_number: Mapped[str] = mapped_column(String(64), nullable=False)
    carrier: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    estimated_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_payments_organization_id"),
        ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            name="fk_payments_order_same_organization",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status in ('pending', 'succeeded', 'failed', 'partially_refunded', 'refunded')",
            name="ck_payments_status",
        ),
        CheckConstraint("amount >= 0", name="ck_payments_amount_nonnegative"),
        CheckConstraint(
            "currency = upper(currency) and length(currency) = 3",
            name="ck_payments_currency",
        ),
        Index("ix_payments_organization_order", "organization_id", "order_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="mock")
    provider_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "payment_id"],
            ["payments.organization_id", "payments.id"],
            name="fk_refunds_payment_same_organization",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status in ('pending', 'requires_approval', 'approved', "
            "'rejected', 'processing', 'succeeded', 'failed')",
            name="ck_refunds_status",
        ),
        CheckConstraint("amount > 0", name="ck_refunds_amount_positive"),
        UniqueConstraint("organization_id", "idempotency_key", name="uq_refunds_idempotency_key"),
        Index("ix_refunds_organization_payment", "organization_id", "payment_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    payment_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class BusinessAction(Base):
    __tablename__ = "business_actions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            ondelete="RESTRICT",
            name="fk_business_actions_order_tenant",
        ),
        UniqueConstraint("organization_id", "idempotency_key", name="uq_business_actions_key"),
        CheckConstraint("kind in ('cancellation', 'refund')", name="ck_business_actions_kind"),
        CheckConstraint(
            "status in ('pending', 'succeeded', 'rejected')", name="ck_business_actions_status"
        ),
        Index("ix_business_actions_org_created", "organization_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id"),
        nullable=False,
    )
    order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    refund_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("refunds.id", ondelete="RESTRICT"),
    )
    requester_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    reviewer_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False)
    decision_note: Mapped[str | None] = mapped_column(String(240))
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationStatus(StrEnum):
    OPEN = "open"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


class ConversationMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class AgentWorkflowStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_conversations_organization_id"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_conversations_customer_same_organization",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status in ('open', 'escalated', 'resolved')",
            name="ck_conversations_status",
        ),
        Index("ix_conversations_organization_updated", "organization_id", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ConversationStatus.OPEN.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class VoiceCall(Base):
    __tablename__ = "voice_calls"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_voice_call_conversation_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id", name="uq_voice_call_tenant_id"),
        UniqueConstraint("conversation_id", name="uq_voice_call_conversation"),
        CheckConstraint(
            "status in ('created', 'connecting', 'in-progress', 'ended')",
            name="ck_voice_call_status",
        ),
        CheckConstraint("channel in ('browser', 'phone')", name="ck_voice_call_channel"),
        ForeignKeyConstraint(
            ["organization_id", "phone_number_config_id"],
            ["voice_phone_numbers.organization_id", "voice_phone_numbers.id"],
            name="fk_voice_call_phone_number_same_organization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "candidate_customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_voice_call_candidate_same_organization",
            ondelete="RESTRICT",
        ),
        Index("ix_voice_calls_org_created", "organization_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    requester_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    provider_call_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, default="browser", server_default="browser"
    )
    phone_number_config_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    candidate_customer_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    phone_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verification_tool_id: Mapped[str | None] = mapped_column(String(128))
    last_verification_result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="created")
    ended_reason: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VoicePhoneNumber(Base):
    __tablename__ = "voice_phone_numbers"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_voice_phone_numbers_tenant_id"),
        UniqueConstraint("provider_number_id", name="uq_voice_phone_numbers_provider_id"),
        ForeignKeyConstraint(
            ["organization_id", "service_user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_voice_phone_number_service_membership",
            ondelete="RESTRICT",
        ),
        Index("ix_voice_phone_numbers_organization_active", "organization_id", "is_active"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    provider_number_id: Mapped[str] = mapped_column(String(128), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(16), nullable=False)
    service_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class VoiceCustomerVerification(Base):
    __tablename__ = "voice_customer_verifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["voice_calls.organization_id", "voice_calls.id"],
            name="fk_voice_verification_call_same_organization",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_voice_verification_customer_same_organization",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "attempt_count >= 0 and attempt_count <= 5",
            name="ck_voice_verification_attempt_count",
        ),
        Index(
            "ix_voice_verifications_org_customer_created",
            "organization_id",
            "customer_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    call_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class VoiceTurn(Base):
    __tablename__ = "voice_turns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["voice_calls.organization_id", "voice_calls.id"],
            name="fk_voice_turn_call_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workflow_id"],
            ["agent_workflows.organization_id", "agent_workflows.id"],
            name="fk_voice_turn_workflow_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint("call_id", "provider_tool_id", name="uq_voice_turn_tool"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    call_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workflow_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_tool_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_messages_organization_id"),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_messages_conversation_same_organization",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "role in ('user', 'assistant')",
            name="ck_conversation_messages_role",
        ),
        Index(
            "ix_messages_organization_conversation_created",
            "organization_id",
            "conversation_id",
            "created_at",
        ),
        UniqueConstraint(
            "organization_id",
            "conversation_id",
            "sequence_number",
            name="uq_conversation_messages_sequence",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence_number: Mapped[int] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentWorkflow(Base):
    __tablename__ = "agent_workflows"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_workflows_organization_id"),
        ForeignKeyConstraint(
            ["organization_id", "requested_by_user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_workflows_requester_same_organization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_workflows_conversation_same_organization",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "trigger_message_id"],
            ["conversation_messages.organization_id", "conversation_messages.id"],
            name="fk_workflows_trigger_message_same_organization",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status in ('queued', 'running', 'completed', 'failed')",
            name="ck_agent_workflows_status",
        ),
        Index(
            "ix_workflows_organization_conversation_created",
            "organization_id",
            "conversation_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    requested_by_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trigger_message_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AgentWorkflowStatus.QUEUED.value
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkflowEvent(Base):
    __tablename__ = "workflow_events"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "workflow_id",
            "sequence_number",
            name="uq_workflow_events_sequence",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workflow_id"],
            ["agent_workflows.organization_id", "agent_workflows.id"],
            name="fk_workflow_events_workflow_same_organization",
            ondelete="CASCADE",
        ),
        Index(
            "ix_workflow_events_organization_workflow_created",
            "organization_id",
            "workflow_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence_number: Mapped[int] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ToolExecution(Base):
    __tablename__ = "tool_executions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workflow_id"],
            ["agent_workflows.organization_id", "agent_workflows.id"],
            name="fk_tool_executions_workflow_same_organization",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_tool_executions_actor_same_organization",
            ondelete="RESTRICT",
        ),
        CheckConstraint("operation in ('read', 'write')", name="ck_tool_executions_operation"),
        CheckConstraint(
            "status in ('succeeded', 'failed')",
            name="ck_tool_executions_status",
        ),
        CheckConstraint("duration_ms >= 0", name="ck_tool_executions_duration_nonnegative"),
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_tool_executions_idempotency_key",
        ),
        Index(
            "ix_tool_executions_organization_workflow_created",
            "organization_id",
            "workflow_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    duration_ms: Mapped[int] = mapped_column(nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    result_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        UniqueConstraint("organization_id", "ticket_number", name="uq_tickets_organization_number"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_tickets_customer_same_organization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            name="fk_tickets_order_same_organization",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status in ('open', 'in_progress', 'waiting_on_customer', 'resolved', 'closed')",
            name="ck_tickets_status",
        ),
        CheckConstraint(
            "priority in ('low', 'normal', 'high', 'urgent')",
            name="ck_tickets_priority",
        ),
        Index("ix_tickets_organization_status", "organization_id", "status"),
        Index("ix_tickets_organization_customer", "organization_id", "customer_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    order_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    ticket_number: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SupportTicketStatus.OPEN.value
    )
    priority: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SupportTicketPriority.NORMAL.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeStore(Base):
    __tablename__ = "knowledge_stores"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_knowledge_stores_organization"),
        CheckConstraint(
            "status in ('pending', 'ready', 'failed')",
            name="ck_knowledge_stores_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_store_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_knowledge_documents_organization_id"),
        ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_knowledge_documents_creator_same_organization",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status in ('active', 'deleting', 'deleted')",
            name="ck_knowledge_documents_status",
        ),
        Index(
            "ix_knowledge_documents_organization_status_created",
            "organization_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeDocumentVersion(Base):
    __tablename__ = "knowledge_document_versions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_knowledge_document_versions_organization_id",
        ),
        UniqueConstraint(
            "organization_id",
            "document_id",
            "version_number",
            name="uq_knowledge_document_versions_number",
        ),
        UniqueConstraint(
            "organization_id",
            "provider_file_id",
            name="uq_knowledge_document_versions_provider_file",
        ),
        ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["knowledge_documents.organization_id", "knowledge_documents.id"],
            name="fk_knowledge_document_versions_document_same_organization",
            ondelete="CASCADE",
        ),
        CheckConstraint("version_number >= 1", name="ck_knowledge_document_versions_number"),
        CheckConstraint("byte_size > 0", name="ck_knowledge_document_versions_byte_size"),
        CheckConstraint(
            "status in ('queued', 'processing', 'ready', 'failed', 'superseded', 'deleted')",
            name="ck_knowledge_document_versions_status",
        ),
        Index(
            "ix_knowledge_document_versions_document_status",
            "organization_id",
            "document_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    provider_file_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
