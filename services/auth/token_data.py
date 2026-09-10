from pydantic import BaseModel


class Token(BaseModel):
    access_token: str
    token_type: str
    # Additive: minted alongside every access token since §9, exchanged at
    # POST /token/refresh for a new pair without a password or MFA code.
    refresh_token: str
