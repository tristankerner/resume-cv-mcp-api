"""The HTML for /oauth/authorize: the login+consent form, and its error page.

Split out of oauth_service.py, which otherwise mixes ~80 lines of presentation
into a service. The rendered HTML must stay byte-identical to what
oauth_service used to produce — tests/test_oauth.py asserts against it.
"""

from html import escape
from typing import ClassVar

from persistence.oauth_client import OAuthClient
from services.auth.html_page import HtmlPage
from services.auth.passkeys.browser_script import PasskeyBrowserScript
from services.auth.scopes import Scopes


class AuthorizePageRenderer:
    # Unlike the docs login's submit (a fetch, since there is no consent
    # decision to preserve), this page still needs the user to click Approve
    # or Deny — so the assertion only fills in the hidden fields the form
    # will carry on submit, and disables the password fields so their
    # `required` attributes do not block that click. `credential`/`loginToken`
    # are in scope where PasskeyBrowserScript splices this in.
    _PASSKEY_SUBMIT: ClassVar[str] = """\
      document.querySelector('input[name="login_token"]').value = loginToken;
      document.querySelector('input[name="passkey_response"]').value =
        JSON.stringify(credential);
      document
        .querySelectorAll('input[name="username"], input[name="password"]')
        .forEach(function (el) {
          el.required = false;
          el.disabled = true;
          var label = el.closest("label");
          if (label) label.style.display = "none";
        });
      btn.replaceWith(
        Object.assign(document.createElement("p"), {
          textContent: "Signed in with a passkey.",
        })
      );
"""

    @classmethod
    def render_error_page(cls, detail: str) -> str:
        return HtmlPage.render(
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
        passkeys_enabled: bool = False,
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
        if passkeys_enabled:
            # Empty placeholders the passkey script fills in client-side, not
            # a credential value.
            hidden_fields["login_token"] = ""  # nosec B105
            hidden_fields["passkey_response"] = ""
        hidden_html = "\n".join(
            f'<input type="hidden" name="{escape(name)}" value="{escape(value)}">'
            for name, value in hidden_fields.items()
        )
        scope_html = "".join(
            f'<span class="scope">{escape(str(s))}</span> ' for s in sorted(scopes)
        )
        error_html = f'<p class="error">{escape(error)}</p>' if error else ""
        passkey_button = (
            '<button type="button" id="passkey">Sign in with a passkey</button>'
            if passkeys_enabled
            else ""
        )
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
        {passkey_button}
      </div>
    </form>
    """
        script = (
            PasskeyBrowserScript.render(
                options_url="/oauth/authorize/passkey/options",
                submit=cls._PASSKEY_SUBMIT,
            )
            if passkeys_enabled
            else ""
        )
        return HtmlPage.render(
            title=f"Authorize {escape(client.client_name)}", body=body, script=script
        )

    @classmethod
    def render_mfa_form(
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
        mfa_token: str,
        error: str | None = None,
    ) -> str:
        """The second step, once the password is accepted and the account
        carries a second factor. Every hidden field the first form carries,
        plus `mfa_token`; `username`/`password` are dropped in favour of
        `code`. Approve/Deny stay, so a user can still back out here."""
        hidden_fields = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": response_type,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "scope": scope,
            "state": state or "",
            "resource": resource or "",
            "mfa_token": mfa_token,
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
    <p>Enter the code from your authenticator app.</p>
    {error_html}
    <form method="post">
      {hidden_html}
      <label>Code<input type="text" name="code" required autofocus
             inputmode="numeric" autocomplete="one-time-code"></label>
      <div class="actions">
        <button type="submit" name="decision" value="approve">Approve</button>
        <button type="submit" name="decision" value="deny">Deny</button>
      </div>
    </form>
    """
        return HtmlPage.render(
            title=f"Authorize {escape(client.client_name)}", body=body
        )
