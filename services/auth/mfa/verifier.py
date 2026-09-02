from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from persistence.base import Clock
from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.auth.mfa.challenge import MfaChallengeContext, MfaChallengeToken
from services.auth.mfa.kinds import MfaMethodKind
from services.auth.mfa.registry import MfaMethodRegistry
from services.config.config_service import ConfigServiceModel


class MfaVerifier:
    """Whether a user's second factor is required, and whether a code
    satisfies it.

    Deliberately separate from MfaService: this runs on the login path with
    no Principal in hand — there is no authenticated caller yet — while
    MfaService is request-scoped behind a Principal and manages enrolment.
    Sharing one class would mean a class that is sometimes authenticated.
    """

    def __init__(self, db: AsyncSession, settings: ConfigServiceModel):
        self.db = db
        self.settings = settings
        self.registry = MfaMethodRegistry(db, settings)

    async def required_kinds(self, user: User) -> list[MfaMethodKind]:
        """The kinds this user has an activated credential for, deduped and
        ordered. Empty means no second factor is demanded."""
        credentials = await MfaCredential.list_active_for_user(self.db, user.id)
        seen: list[MfaMethodKind] = []
        for credential in credentials:
            try:
                kind = MfaMethodKind(credential.kind)
            except ValueError:
                continue
            if kind not in seen:
                seen.append(kind)
        return seen

    async def is_enrolled(self, user: User) -> bool:
        return bool(await self.required_kinds(user))

    async def verify(self, user: User, code: str) -> bool:
        """Try `code` against every activated credential the user holds.

        Tried rather than selected: the caller types a code, not a method.
        A backup code and a TOTP code are different shapes, but sniffing the
        shape is a guess, and every candidate check here is one HMAC.

        Mutates whichever credential accepted it (replay step, or a spent
        backup code) and commits — this is the end of the login transaction
        and there is nothing else pending.
        """
        now = Clock.utcnow()
        satisfied = False
        for credential in await MfaCredential.list_active_for_user(self.db, user.id):
            method = self.registry.for_credential(credential)
            if method is None:
                continue
            if await method.verify(credential, code, now):
                satisfied = True
                break
        if satisfied:
            await self.db.commit()
        return satisfied

    async def user_from_challenge(
        self, token: str, context: MfaChallengeContext, binding: str | None = None
    ) -> User | None:
        """The user named by a valid, unexpired MFA challenge token minted
        for `context` and `binding`, or None for any failure at all —
        including one whose account was deactivated, or whose password
        changed, since the challenge was minted.

        See `MfaChallengeToken.mint` for what `binding` narrows.
        """
        payload = MfaChallengeToken._validated_payload(
            self.settings, token, context, binding
        )
        if payload is None:
            return None
        subject = payload.get("sub")
        if not isinstance(subject, str):
            return None
        try:
            user_id = int(subject)
        except ValueError:
            return None

        user = await User.get_user_by_id(self.db, user_id)
        if user is None or not user.active:
            return None
        if payload.get("pwb") != MfaChallengeToken.password_binding(user):
            return None
        return user
