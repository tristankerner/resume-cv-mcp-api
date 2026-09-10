from pydantic import BaseModel


class AuthCapabilities(BaseModel):
    """What this deployment's auth surface supports, read by a client before
    it ever logs in — see AuthRouter.capabilities."""

    passkeys: bool
