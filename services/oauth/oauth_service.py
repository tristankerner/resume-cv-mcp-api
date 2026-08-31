"""Orchestrates the new endpoints. Routers stay thin — one call each, plus
turning the exceptions in `services.oauth.exceptions` into the right kind of
response — everything with a decision to make lives here."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.oauth_client import OAuthClient
from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.mfa.challenge import MfaChallengeContext, MfaChallengeToken
from services.auth.mfa.verifier import MfaVerifier
from services.auth.scopes import ScopeResolver, Scopes
from services.config.config_service import ConfigService, ConfigServiceModel
from services.database.database_service import DatabaseService
from services.oauth.clients import OAuthClientRegistry
from services.oauth.codes import AuthorizationCodeStore
from services.oauth.dtos import (
    AuthorizationServerMetadata,
    ClientRegistrationRequest,
    ClientRegistrationResponse,
    TokenResponse,
)
from services.oauth.exceptions import (
    AuthorizeFatalError,
    AuthorizeLoginFailed,
    AuthorizeMfaRequired,
    AuthorizeRedirectError,
    OAuthErrors,
)
from services.oauth.redirect_allowlist import RedirectAllowlist
from services.oauth.scopes import OAUTH_ISSUABLE_SCOPES
from services.oauth.tokens import TokenIssuer
from services.service_interface import ServiceProviderInterface


class OAuthService(ServiceProviderInterface):
    def __init__(self, db: AsyncSession, config_service: ConfigService):
        self.db = db
        self.config_service = config_service
        self.settings: ConfigServiceModel = config_service.settings

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        config_service: Annotated[ConfigService, Depends(ConfigService.get_with_deps)],
    ) -> OAuthService:
        return OAuthService(db, config_service)

    @staticmethod
    def _append_query(url: str, extra: dict[str, str | None]) -> str:
        parts = urlparse(url)
        query = dict(parse_qsl(parts.query))
        query.update({k: v for k, v in extra.items() if v is not None})
        return urlunparse(parts._replace(query=urlencode(query)))

    # --- Discovery ------------------------------------------------------

    def authorization_server_metadata(self) -> AuthorizationServerMetadata:
        settings = self.settings
        base = settings.public_base_url_str
        return AuthorizationServerMetadata(
            # The AnyHttpUrl rendering, trailing slash and all, so this is
            # byte identical to the `authorization_servers` entry in the
            # protected resource document (see main.py) — which is the string
            # a client took as the issuer identifier before fetching this.
            # RFC 8414 §3.3 makes that identity load-bearing: "If these values
            # are not identical, the data contained in the response MUST NOT
            # be used." A client that compares without normalising would
            # otherwise discard this document and abandon the flow.
            #
            # Endpoints below keep using `base`, which has the slash
            # stripped, so they do not come out with a doubled one.
            issuer=str(settings.public_base_url),
            authorization_endpoint=f"{base}/oauth/authorize",
            token_endpoint=f"{base}/oauth/token",
            # Omitted entirely when registration is closed, which is how a
            # client learns to expect a pre-registered client_id rather than
            # trying to register and failing.
            registration_endpoint=(
                f"{base}/oauth/register"
                if settings.oauth_registration_enabled
                else None
            ),
            revocation_endpoint=f"{base}/oauth/revoke",
            scopes_supported=sorted(str(scope) for scope in OAUTH_ISSUABLE_SCOPES),
            # With registration closed every client is pre-registered, and
            # `python -m register_oauth_client` issues a secret unless asked
            # not to — so advertising "none" would be advertising a shape
            # this deployment does not normally hand out. It stays supported
            # for a public client created with --public.
            token_endpoint_auth_methods_supported=(
                ["client_secret_post", "none"]
                if settings.oauth_registration_enabled
                else ["client_secret_post"]
            ),
        )

    # --- Registration -----------------------------------------------------

    async def register_client(
        self, request: ClientRegistrationRequest
    ) -> ClientRegistrationResponse:
        """Anonymous DCR, which is off unless `OAUTH_REGISTRATION_ENABLED`
        says otherwise — the router refuses before reaching here, so this is
        the open path only."""
        return await OAuthClientRegistry(self.db).register(
            request, self.settings.oauth_allowed_redirect_hosts
        )

    # --- Authorize ----------------------------------------------------

    async def _load_client_and_validate_redirect(
        self, client_id: str, redirect_uri: str
    ) -> OAuthClient:
        """The one check that must never redirect on failure: an unvalidated
        redirect_uri gets an HTML page, not a bounce to a URI nobody has
        checked.

        Re-validated against the live allowlist, not just the registered
        value: removing a host from `OAUTH_ALLOWED_REDIRECT_HOSTS` is meant to
        take effect immediately, including for clients that registered while
        it was still listed.
        """
        client = await OAuthClientRegistry(self.db).get(client_id)
        if client is None:
            raise AuthorizeFatalError("Unknown client_id.")
        if redirect_uri not in client.redirect_uris:
            raise AuthorizeFatalError(
                "redirect_uri does not match any URI registered for this client."
            )
        if not RedirectAllowlist(self.settings.oauth_allowed_redirect_hosts).allows(
            redirect_uri
        ):
            raise AuthorizeFatalError("redirect_uri's host is no longer allowed.")
        return client

    @staticmethod
    def _validate_protocol_params(
        response_type: str,
        code_challenge: str | None,
        code_challenge_method: str | None,
        scope: str | None,
        state: str | None,
    ) -> frozenset[Scopes]:
        """Everything past this point is a validated-redirect_uri failure, so
        it answers by redirect (RFC 6749 §4.1.2.1), not by page or by JSON."""
        if response_type != "code":
            raise AuthorizeRedirectError(
                "unsupported_response_type", "Only 'code' is supported.", state
            )
        if not code_challenge:
            raise AuthorizeRedirectError(
                "invalid_request", "code_challenge is required.", state
            )
        if code_challenge_method != "S256":
            # `plain` is never accepted, not even as a fallback.
            raise AuthorizeRedirectError(
                "invalid_request", "code_challenge_method must be S256.", state
            )
        # Capped at what an OAuth grant may ever carry, not merely at what the
        # user holds — see services/oauth/scopes.py. A client that asks for
        # more than the metadata advertises is narrowed rather than refused,
        # since the extra scopes are ones no MCP tool uses and the request is
        # still satisfiable without them.
        requested = (
            ScopeResolver.parse(str(scope or "").split()) & OAUTH_ISSUABLE_SCOPES
        )
        if not requested:
            raise AuthorizeRedirectError(
                "invalid_scope",
                "No requested scope is available over OAuth. This endpoint "
                f"issues only: {' '.join(sorted(str(s) for s in OAUTH_ISSUABLE_SCOPES))}.",
                state,
            )
        return requested

    async def prepare_authorize(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        response_type: str,
        code_challenge: str | None,
        code_challenge_method: str | None,
        scope: str | None,
        state: str | None,
    ) -> tuple[OAuthClient, frozenset[Scopes]]:
        """The GET validation, reused as the first step of the POST — see
        `complete_authorize`."""
        client = await self._load_client_and_validate_redirect(client_id, redirect_uri)
        requested = self._validate_protocol_params(
            response_type, code_challenge, code_challenge_method, scope, state
        )
        return client, requested

    async def _resolve_user(
        self,
        auth_service: AuthService,
        mfa_token: str | None,
        code: str | None,
        username: str,
        password: str,
        client_id: str,
    ) -> User:
        """The password step, or the second-factor step redeeming its
        challenge — whichever this submission is.

        Raises `AuthorizeMfaRequired` with `detail=None` the first time an
        enrolled account's password is accepted (render the code form), and
        with a detail message plus a freshly minted token on a rejected code
        (re-render it, so the next attempt gets a full window rather than
        racing whatever was left of the old one).

        `register_mfa_failure` may itself raise `AuthErrors.account_locked`
        (a 429 `HTTPException`), which then escapes this handler as a plain
        error response rather than a rendered page — acceptable and honest,
        not caught here.

        Every challenge here is bound to `client_id`, so one issued while
        authorizing a client cannot be redeemed while authorizing a
        different one — the consent shown on the first page is not consent
        to whatever the second page asked for.
        """
        verifier = MfaVerifier(self.db, self.settings)

        if mfa_token:
            user = await verifier.user_from_challenge(
                mfa_token, MfaChallengeContext.OAUTH, binding=client_id
            )
            if user is None:
                raise AuthorizeLoginFailed("That login attempt expired. Try again.")

            auth_service.raise_if_locked(user)

            if not await verifier.verify(user, code or ""):
                await auth_service.register_mfa_failure(user)
                new_token, _expires_in = MfaChallengeToken.mint(
                    self.settings, user, MfaChallengeContext.OAUTH, binding=client_id
                )
                raise AuthorizeMfaRequired(new_token, "That code is not valid.")
            await auth_service.clear_login_failures(user)
            return user

        user = await auth_service.authenticate_user(username, password)
        if not user:
            raise AuthorizeLoginFailed("Incorrect username or password.")
        if await verifier.is_enrolled(user):
            token, _expires_in = MfaChallengeToken.mint(
                self.settings, user, MfaChallengeContext.OAUTH, binding=client_id
            )
            raise AuthorizeMfaRequired(token)
        return user

    async def complete_authorize(
        self,
        request: Request,
        *,
        client_id: str,
        redirect_uri: str,
        response_type: str,
        code_challenge: str | None,
        code_challenge_method: str | None,
        scope: str | None,
        state: str | None,
        resource: str | None,
        username: str,
        password: str,
        approved: bool,
        mfa_token: str | None = None,
        code: str | None = None,
    ) -> str:
        """The POST handler's whole body. Returns the redirect URL on success.

        Raises `AuthorizeFatalError` (render a page), `AuthorizeRedirectError`
        (redirect with `error=`), `AuthorizeLoginFailed` (re-show the form),
        or `AuthorizeMfaRequired` (show the code form) — the router picks the
        response shape from which one it catches.
        """
        client, requested = await self.prepare_authorize(
            client_id=client_id,
            redirect_uri=redirect_uri,
            response_type=response_type,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
            state=state,
        )

        if not approved:
            raise AuthorizeRedirectError(
                "access_denied", "The resource owner denied the request.", state
            )
        # prepare_authorize's _validate_protocol_params raises on a falsy
        # code_challenge, so this always holds by the time execution reaches
        # here — asserted rather than left implicit, for the type checker and
        # for the reader.

        if not code_challenge:
            raise AuthorizeLoginFailed("incorrect_code_challenge")

        auth_service = AuthService(self.db, None, self.config_service, request)
        user = await self._resolve_user(
            auth_service, mfa_token, code, username, password, client.client_id
        )

        # Requested scopes are intersected with the user's role scopes before
        # the grant is recorded — a user cannot consent to more than they hold.
        granted = requested & await ScopeResolver.for_roles(self.db, user.roles)
        if not granted:
            raise AuthorizeRedirectError(
                "invalid_scope",
                "None of the requested scopes are available to this account.",
                state,
            )

        code = await AuthorizationCodeStore(self.db).issue(
            client_id=client.client_id,
            user_id=user.id,
            redirect_uri=redirect_uri,
            scopes=granted,
            code_challenge=code_challenge,  # validated non-None in prepare_authorize
            resource=resource,
        )
        return self._append_query(redirect_uri, {"code": code, "state": state})

    # --- Token --------------------------------------------------------------

    async def exchange_token(
        self,
        *,
        grant_type: str,
        client_id: str | None,
        client_secret: str | None,
        code: str | None,
        redirect_uri: str | None,
        code_verifier: str | None,
        refresh_token: str | None,
    ) -> TokenResponse:
        if not client_id:
            raise OAuthErrors.invalid_client()
        registry = OAuthClientRegistry(self.db)
        client = await registry.get(client_id)
        if client is None or not registry.verify_secret(client, client_secret):
            raise OAuthErrors.invalid_client()

        token_issuer = TokenIssuer(self.db, self.settings)

        if grant_type == "authorization_code":
            if not code or not redirect_uri or not code_verifier:
                raise OAuthErrors.invalid_grant(
                    "code, redirect_uri and code_verifier are required."
                )
            record = await AuthorizationCodeStore(self.db).consume(
                code=code,
                client_id=client.client_id,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
            )
            granted = ScopeResolver.parse(record.scopes)
            access_token, expires_in = token_issuer.mint_access_token(
                user_id=record.user_id, scopes=granted, client_id=client.client_id
            )
            # Commits the code's consumed_at alongside this insert, in the
            # same transaction — see AuthorizationCodeStore.consume's
            # docstring.
            new_refresh_token = await token_issuer.issue_refresh_token(
                client_id=client.client_id,
                user_id=record.user_id,
                scopes=granted,
                resource=record.resource,
            )
            return TokenResponse(
                access_token=access_token,
                expires_in=expires_in,
                refresh_token=new_refresh_token,
                scope=" ".join(sorted(str(s) for s in granted)),
            )

        if grant_type == "refresh_token":
            if not refresh_token:
                raise OAuthErrors.invalid_grant("refresh_token is required.")
            record, new_raw = await token_issuer.rotate(
                presented_token=refresh_token, client_id=client.client_id
            )
            user = await User.get_user_by_id(self.db, record.user_id)
            if user is None or not user.active:
                raise OAuthErrors.invalid_grant("Account is no longer available.")
            # Re-intersected with current role scopes, not just carried over
            # from the stored grant — see ScopeResolver.narrow.
            current = await ScopeResolver.narrow(
                self.db, ScopeResolver.parse(record.scopes), user.roles
            )
            access_token, expires_in = token_issuer.mint_access_token(
                user_id=user.id, scopes=current, client_id=client.client_id
            )
            return TokenResponse(
                access_token=access_token,
                expires_in=expires_in,
                refresh_token=new_raw,
                scope=" ".join(sorted(str(s) for s in current)),
            )

        raise OAuthErrors.unsupported_grant_type(
            f"Unsupported grant_type: {grant_type!r}"
        )

    async def revoke_token(
        self,
        *,
        token: str,
        client_id: str | None,
        client_secret: str | None,
    ) -> None:
        if not client_id:
            raise OAuthErrors.invalid_client()
        registry = OAuthClientRegistry(self.db)
        client = await registry.get(client_id)
        if client is None or not registry.verify_secret(client, client_secret):
            raise OAuthErrors.invalid_client()
        await TokenIssuer(self.db, self.settings).revoke(
            presented_token=token, client_id=client.client_id
        )
