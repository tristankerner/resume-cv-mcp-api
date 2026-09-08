"""teach the skill catalogue example about job codes

Content only: `ResumeSkill` is unchanged, so the skill document's *schema*
version stays 2 and no stored document becomes stale. What moves is the
example a new account is seeded with, which is why this needs a migration at
all — `tests/test_catalogue_examples.py` holds the `document_schema` row and
`examples/resume.skill.example.json` to being identical, deliberately, so that
changing what a new account starts with costs a migration rather than a quiet
file edit.

The additions tell a tailoring run to search `job_code` before recording, to
pass it when the posting names one, and to stop and say so when the code is
already on file — the same job reaching the user through a second recruiter
being the thing worth noticing.

Revision ID: c0d3b18e4a52
Revises: a71e4c05d938
Create Date: 2026-09-07 20:40:00.000000

"""
import json
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c0d3b18e4a52"
down_revision: Union[str, Sequence[str], None] = "a71e4c05d938"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SKILL_VERSION = 2
SNAPSHOTS = Path(__file__).resolve().parent / "schema_snapshots"

# Read from a frozen snapshot beside this file rather than from
# `examples/resume.skill.example.json`, so replaying this revision produces
# what it produced the day it was written even after the example moves on
# again. `eb599ee50641` established the pattern and the v2_1 file it wrote.
NEW_EXAMPLE_FILE = SNAPSHOTS / "v2_2_skill.example.json"
OLD_EXAMPLE_FILE = SNAPSHOTS / "v2_1_skill.example.json"


def _set_example(example: dict) -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE document_schema SET example = :example "
            "WHERE version = :version AND document_type = 'skill'"
        ),
        {"example": json.dumps(example), "version": SKILL_VERSION},
    )


def upgrade() -> None:
    """Upgrade schema."""
    _set_example(json.loads(NEW_EXAMPLE_FILE.read_text()))


def downgrade() -> None:
    """Downgrade schema."""
    _set_example(json.loads(OLD_EXAMPLE_FILE.read_text()))
