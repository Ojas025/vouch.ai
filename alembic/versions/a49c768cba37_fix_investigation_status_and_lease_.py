"""fix investigation status and lease constraints

Revision ID: a49c768cba37
Revises: 96e9713cce80
Create Date: 2026-11-20 11:38:44.653750

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a49c768cba37'
down_revision: Union[str, Sequence[str], None] = '96e9713cce80'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE investigations SET status = lower(status)")
    op.execute("ALTER TABLE investigations DROP CONSTRAINT ck_inv_execution_status")
    op.execute(
        "ALTER TABLE investigations ADD CONSTRAINT ck_inv_execution_status "
        "CHECK (status IN ('received','processing','completed','dead','failed','retry'))"
    )
    op.execute("ALTER TABLE investigations DROP CONSTRAINT ck_inv_lease_consistency")
    op.execute(
        "ALTER TABLE investigations ADD CONSTRAINT ck_inv_lease_consistency "
        "CHECK ((status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_renewed_at IS NOT NULL) "
        "OR (status <> 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL AND lease_renewed_at IS NULL))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE investigations DROP CONSTRAINT ck_inv_lease_consistency")
    op.execute(
        "ALTER TABLE investigations ADD CONSTRAINT ck_inv_lease_consistency "
        "CHECK ((status = 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL AND lease_renewed_at IS NULL) "
        "OR (status <> 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_renewed_at IS NOT NULL))"
    )
    op.execute("ALTER TABLE investigations DROP CONSTRAINT ck_inv_execution_status")
    op.execute(
        "ALTER TABLE investigations ADD CONSTRAINT ck_inv_execution_status "
        "CHECK (status IN ('RECEIVED','PROCESSING','COMPLETED','DEAD','FAILED','RETRY'))"
    )
