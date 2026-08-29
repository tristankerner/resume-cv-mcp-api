from datetime import datetime

from sqlalchemy import JSON, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from .base import SQAlchemyBase, utcnow


class OAuthClient(SQAlchemyBase):
    """A client registered through Dynamic Client Registration (RFC 7591).

    Registration is deliberately credential-free — see
    services/oauth/redirect_allowlist.py for what bounds it instead. `id` is a
    surrogate nothing else references; `client_id` is the public identifier
    every other OAuth table and every wire exchange uses, so it carries its
    own unique index rather than the surrogate key.
    """

    __tablename__ = "oauth_clients"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(unique=True, index=True, nullable=False)
    # None for a public client (PKCE, no secret) — a DCR request with
    # token_endpoint_auth_method "none", which is the expected shape for an
    # MCP client that cannot keep a secret.
    client_secret_hash: Mapped[str | None]
    client_name: Mapped[str] = mapped_column(nullable=False)
    redirect_uris: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), nullable=False
    )
    grant_types: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), nullable=False
    )
    response_types: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), nullable=False
    )
    token_endpoint_auth_method: Mapped[str] = mapped_column(nullable=False)
    scope: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    client_secret_expires_at: Mapped[datetime | None]

    def __repr__(self) -> str:
        return f"OAuthClient(client_id={self.client_id!r}, name={self.client_name!r})"

    @staticmethod
    async def get_by_client_id(db: AsyncSession, client_id: str) -> OAuthClient | None:
        return (
            (
                await db.execute(
                    select(OAuthClient).where(OAuthClient.client_id == client_id)
                )
            )
            .scalars()
            .first()
        )
