"""create outbox_events table

Adds the table the transactional outbox has been writing to but which was never
migrated, together with the nullable ``trace_context`` column the relay uses to
continue the originating request's trace.

Column definitions mirror ``vouch.db.models.OutboxEvent`` exactly so
``alembic check`` reports no drift. In particular ``published_at`` is NOT NULL,
matching the model; that is a pre-existing defect (``tests/integration/
test_db_constraints.py`` pins it as a strict xfail) and is deliberately not
fixed here.

Revision ID: 8c1d4f0a9b23
Revises: 3424ff6af832
Create Date: 2026-10-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '8c1d4f0a9b23'
down_revision: Union[str, Sequence[str], None] = '3424ff6af832'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'outbox_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('channel', sa.String(length=20), nullable=False),
        sa.Column('idempotency_key', sa.String(length=128), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('trace_context', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('outbox_events')