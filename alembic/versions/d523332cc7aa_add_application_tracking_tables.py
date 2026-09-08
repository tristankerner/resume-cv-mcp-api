"""add application tracking tables

Seven tables for the application-tracking feature: `companies`,
`company_relationships`, `company_stack_items`, `contacts`, `applications`,
`application_events`, `application_attachments`. See
APPLICATION_TRACKING_PLAN.md section 2 for the full data model.

`applications` carries three composite foreign keys into
`documents(created_by, name, revision_id)`, deliberately with no `ON DELETE`
clause - `services/document/document_service.py` clears or retargets those
references by hand before a document delete or rename, so the constraint is
never violated by application code. See section 4.8.

`ON DELETE CASCADE` / `SET NULL` on the other foreign keys is declared for
Postgres correctness. SQLite does not enforce foreign keys in this
application (`PRAGMA foreign_keys` stays off - see section 0 rule 7), so the
service layer deletes dependents explicitly on both dialects.

Revision ID: d523332cc7aa
Revises: 78480d80f589
Create Date: 2026-09-07 14:07:52.883629

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd523332cc7aa'
down_revision: Union[str, Sequence[str], None] = '78480d80f589'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("normalized_name", sa.String(), nullable=False),
        sa.Column("website", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("personal_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id", "normalized_name", name="uq_companies_user_normalized_name"
        ),
    )
    op.create_index("ix_companies_user_name", "companies", ["user_id", "name"])

    op.create_table(
        "company_relationships",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "from_company_id",
            sa.Integer(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_company_id",
            sa.Integer(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "from_company_id <> to_company_id",
            name="ck_company_relationships_not_self",
        ),
        sa.UniqueConstraint(
            "user_id",
            "from_company_id",
            "to_company_id",
            "type",
            name="uq_company_relationships_edge",
        ),
    )
    op.create_index(
        "ix_company_relationships_user_from",
        "company_relationships",
        ["user_id", "from_company_id"],
    )
    op.create_index(
        "ix_company_relationships_user_to",
        "company_relationships",
        ["user_id", "to_company_id"],
    )

    op.create_table(
        "company_stack_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("normalized_name", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "company_id",
            "normalized_name",
            name="uq_company_stack_items_company_name",
        ),
    )
    op.create_index(
        "ix_company_stack_items_user_company",
        "company_stack_items",
        ["user_id", "company_id"],
    )

    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("first_name", sa.String(), nullable=True),
        sa.Column("last_name", sa.String(), nullable=True),
        sa.Column("normalized_name", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("personal_note", sa.Text(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "rating IS NULL OR (rating BETWEEN 1 AND 10)",
            name="ck_contacts_rating_range",
        ),
    )
    op.create_index("ix_contacts_user_company", "contacts", ["user_id", "company_id"])
    op.create_index(
        "ix_contacts_user_name", "contacts", ["user_id", "last_name", "first_name"]
    )
    op.create_index("ix_contacts_user_email", "contacts", ["user_id", "email"])

    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("job_title", sa.String(), nullable=True),
        sa.Column("normalized_job_title", sa.String(), nullable=True),
        sa.Column("resume_document_name", sa.String(), nullable=True),
        sa.Column("resume_revision_id", sa.Integer(), nullable=True),
        sa.Column("metadata_document_name", sa.String(), nullable=True),
        sa.Column("metadata_revision_id", sa.Integer(), nullable=True),
        sa.Column("skill_document_name", sa.String(), nullable=True),
        sa.Column("skill_revision_id", sa.Integer(), nullable=True),
        sa.Column("resume_label", sa.String(), nullable=True),
        sa.Column("initial_prompt_text", sa.Text(), nullable=True),
        sa.Column("job_description", sa.Text(), nullable=True),
        sa.Column("date_submitted", sa.Date(), nullable=True),
        sa.Column(
            "manually_modified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("modification_note", sa.Text(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("system", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "resume_document_name", "resume_revision_id"],
            ["documents.created_by", "documents.name", "documents.revision_id"],
            name="fk_applications_resume_revision",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "metadata_document_name", "metadata_revision_id"],
            ["documents.created_by", "documents.name", "documents.revision_id"],
            name="fk_applications_metadata_revision",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "skill_document_name", "skill_revision_id"],
            ["documents.created_by", "documents.name", "documents.revision_id"],
            name="fk_applications_skill_revision",
        ),
        sa.CheckConstraint(
            "(resume_document_name IS NULL) = (resume_revision_id IS NULL)",
            name="ck_applications_resume_ref_complete",
        ),
        sa.CheckConstraint(
            "(metadata_document_name IS NULL) = (metadata_revision_id IS NULL)",
            name="ck_applications_metadata_ref_complete",
        ),
        sa.CheckConstraint(
            "(skill_document_name IS NULL) = (skill_revision_id IS NULL)",
            name="ck_applications_skill_ref_complete",
        ),
    )
    op.create_index(
        "ix_applications_user_submitted", "applications", ["user_id", "date_submitted"]
    )
    op.create_index(
        "ix_applications_user_company", "applications", ["user_id", "company_id"]
    )
    op.create_index(
        "ix_applications_user_status", "applications", ["user_id", "status"]
    )
    op.create_index(
        "ix_applications_user_created", "applications", ["user_id", "created_at"]
    )

    op.create_table(
        "application_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column(
            "contact_id",
            sa.Integer(),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "rating IS NULL OR (rating BETWEEN 1 AND 10)",
            name="ck_application_events_rating_range",
        ),
    )
    op.create_index(
        "ix_application_events_user_app_occurred",
        "application_events",
        ["user_id", "application_id", "occurred_at"],
    )
    op.create_index(
        "ix_application_events_user_occurred",
        "application_events",
        ["user_id", "occurred_at"],
    )
    op.create_index(
        "ix_application_events_user_contact",
        "application_events",
        ["user_id", "contact_id"],
    )

    op.create_table(
        "application_attachments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("content_base64", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "application_id",
            "sha256",
            name="uq_application_attachments_sha",
        ),
    )
    op.create_index(
        "ix_application_attachments_user_app",
        "application_attachments",
        ["user_id", "application_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("application_attachments")
    op.drop_table("application_events")
    op.drop_table("applications")
    op.drop_table("contacts")
    op.drop_table("company_stack_items")
    op.drop_table("company_relationships")
    op.drop_table("companies")
