"""add oauth tables

Three tables for OAuth 2.1 dynamic client registration and the
authorization-code and refresh-token grants. No change
to any existing table; the new access-token type these enable is a JWT
distinguished by a `token_use` claim, which needs no schema of its own.

Revision ID: c926171a328a
Revises: 7c2b41f9ae08
Create Date: 2026-08-26 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c926171a328a"
down_revision: Union[str, Sequence[str], None] = "7c2b41f9ae08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("client_secret_hash", sa.String(), nullable=True),
        sa.Column("client_name", sa.String(), nullable=False),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("grant_types", sa.JSON(), nullable=False),
        sa.Column("response_types", sa.JSON(), nullable=False),
        sa.Column("token_endpoint_auth_method", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("client_secret_expires_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id"),
    )
    op.create_index(
        op.f("ix_oauth_clients_client_id"), "oauth_clients", ["client_id"], unique=True
    )

    op.create_table(
        "oauth_authorization_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("code_challenge", sa.String(), nullable=False),
        sa.Column("code_challenge_method", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["oauth_clients.client_id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
    )
    op.create_index(
        op.f("ix_oauth_authorization_codes_code_hash"),
        "oauth_authorization_codes",
        ["code_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_oauth_authorization_codes_client_id"),
        "oauth_authorization_codes",
        ["client_id"],
    )
    op.create_index(
        op.f("ix_oauth_authorization_codes_user_id"),
        "oauth_authorization_codes",
        ["user_id"],
    )

    op.create_table(
        "oauth_refresh_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("resource", sa.String(), nullable=True),
        sa.Column("grant_id", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("rotated_to_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["oauth_clients.client_id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["rotated_to_id"], ["oauth_refresh_tokens.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        op.f("ix_oauth_refresh_tokens_token_hash"),
        "oauth_refresh_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_oauth_refresh_tokens_client_id"), "oauth_refresh_tokens", ["client_id"]
    )
    op.create_index(
        op.f("ix_oauth_refresh_tokens_user_id"), "oauth_refresh_tokens", ["user_id"]
    )
    op.create_index(
        op.f("ix_oauth_refresh_tokens_grant_id"), "oauth_refresh_tokens", ["grant_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_oauth_refresh_tokens_grant_id"), table_name="oauth_refresh_tokens"
    )
    op.drop_index(
        op.f("ix_oauth_refresh_tokens_user_id"), table_name="oauth_refresh_tokens"
    )
    op.drop_index(
        op.f("ix_oauth_refresh_tokens_client_id"), table_name="oauth_refresh_tokens"
    )
    op.drop_index(
        op.f("ix_oauth_refresh_tokens_token_hash"), table_name="oauth_refresh_tokens"
    )
    op.drop_table("oauth_refresh_tokens")

    op.drop_index(
        op.f("ix_oauth_authorization_codes_user_id"),
        table_name="oauth_authorization_codes",
    )
    op.drop_index(
        op.f("ix_oauth_authorization_codes_client_id"),
        table_name="oauth_authorization_codes",
    )
    op.drop_index(
        op.f("ix_oauth_authorization_codes_code_hash"),
        table_name="oauth_authorization_codes",
    )
    op.drop_table("oauth_authorization_codes")

    op.drop_index(op.f("ix_oauth_clients_client_id"), table_name="oauth_clients")
    op.drop_table("oauth_clients")
