"""The HTML for /oauth/authorize: the login+consent form, and its error page.

Split out of oauth_service.py, which otherwise mixes ~80 lines of presentation
into a service. The rendered HTML must stay byte-identical to what
oauth_service used to produce — tests/test_oauth.py asserts against it.
"""

from html import escape
from typing import ClassVar

from persistence.oauth_client import OAuthClient
from services.auth.scopes import Scopes


class AuthorizePageRenderer:
    _PAGE: ClassVar[str] = """\
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 28rem; margin: 4rem auto;
         padding: 0 1rem; color: #1a1a1a; }}
  .scope {{ font-family: monospace; background: #f0f0f0; padding: 0.1rem 0.4rem;
           border-radius: 0.25rem; }}
  label {{ display: block; margin-top: 0.75rem; }}
  input {{ width: 100%; padding: 0.4rem; margin-top: 0.25rem; box-sizing: border-box; }}
  .actions {{ margin-top: 1.5rem; display: flex; gap: 0.5rem; }}
  button {{ padding: 0.5rem 1rem; }}
  .error {{ color: #b00020; margin-top: 0.75rem; }}
</style>
</head>
<body>{body}</body>
</html>
"""

    @classmethod
    def render_error_page(cls, detail: str) -> str:
        return cls._PAGE.format(
            title="Authorization error",
            body=f"<h1>Authorization error</h1><p>{escape(detail)}</p>",
        )

    @classmethod
    def render_authorize_form(
        cls,
        *,
        client: OAuthClient,
        scopes: frozenset[Scopes],
        client_id: str,
        redirect_uri: str,
        response_type: str,
        code_challenge: str,
        code_challenge_method: str,
        scope: str,
        state: str | None,
        resource: str | None,
        error: str | None = None,
    ) -> str:
        hidden_fields = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": response_type,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "scope": scope,
            "state": state or "",
            "resource": resource or "",
        }
        hidden_html = "\n".join(
            f'<input type="hidden" name="{escape(name)}" value="{escape(value)}">'
            for name, value in hidden_fields.items()
        )
        scope_html = "".join(
            f'<span class="scope">{escape(str(s))}</span> ' for s in sorted(scopes)
        )
        error_html = f'<p class="error">{escape(error)}</p>' if error else ""
        body = f"""
    <h1>{escape(client.client_name)}</h1>
    <p>This application is requesting access to:</p>
    <p>{scope_html}</p>
    {error_html}
    <form method="post">
      {hidden_html}
      <label>Username<input type="text" name="username" required autofocus></label>
      <label>Password<input type="password" name="password" required></label>
      <div class="actions">
        <button type="submit" name="decision" value="approve">Approve</button>
        <button type="submit" name="decision" value="deny">Deny</button>
      </div>
    </form>
    """
        return cls._PAGE.format(
            title=f"Authorize {escape(client.client_name)}", body=body
        )
