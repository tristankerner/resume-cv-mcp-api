from typing import Literal

from pydantic import BaseModel

from services.auth.mfa.kinds import MfaMethodKind


class MfaRequiredResponse(BaseModel):
    """The first step's answer when the account has a second factor.

    `mfa_required` is a literal True rather than a bool so a client can
    discriminate the union on it without inspecting which keys are present.

    Status 200, not 401: the password *was* correct, and the request
    succeeded and produced a next step. A 401 here would make a client's
    session-clearing error handling treat this like an expired session,
    which is wrong.
    """

    mfa_required: Literal[True] = True
    mfa_token: str
    methods: list[MfaMethodKind]
    expires_in: int
