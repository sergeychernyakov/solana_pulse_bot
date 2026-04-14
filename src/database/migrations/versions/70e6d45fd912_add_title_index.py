"""add_title_index

Revision ID: 70e6d45fd912
Revises: 619d7e413fa3
Create Date: 2025-10-23 13:01:50.462814

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "70e6d45fd912"
down_revision: Union[str, None] = "619d7e413fa3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add index to tasks.title for improved search performance."""
    op.create_index("ix_tasks_title", "tasks", ["title"])


def downgrade() -> None:
    """Remove index from tasks.title."""
    op.drop_index("ix_tasks_title", "tasks")
