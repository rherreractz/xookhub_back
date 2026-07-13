"""add source_reference and room_id to flashcards

Revision ID: 0002_add_flashcards_fields
Revises: 0001_enable_pgvector
Create Date: 2026-07-06 10:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0002_add_flashcards_fields'
down_revision: Union[str, None] = '0001_enable_pgvector'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Agregar como nullable primero
    op.add_column('flashcards', sa.Column('room_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('flashcards', sa.Column('source_reference', sa.Text(), nullable=True))

    # 2. Backfill room_id desde el documento padre
    op.execute("""
        UPDATE flashcards
        SET room_id = documents.room_id
        FROM documents
        WHERE flashcards.document_id = documents.id
    """)

    # 3. Ahora sí, aplicar el NOT NULL y la FK
    op.alter_column('flashcards', 'room_id', nullable=False)
    op.create_foreign_key(
        'fk_flashcards_room_id', 'flashcards', 'study_rooms',
        ['room_id'], ['id'], ondelete='CASCADE'
    )
    op.create_index('ix_flashcards_room_id', 'flashcards', ['room_id'])


def downgrade() -> None:
    op.drop_index('ix_flashcards_room_id', table_name='flashcards')
    op.drop_constraint('fk_flashcards_room_id', 'flashcards', type_='foreignkey')
    op.drop_column('flashcards', 'source_reference')
    op.drop_column('flashcards', 'room_id')