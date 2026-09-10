"""The two forms in front of the production documentation: a password, then
— for an MFA-enrolled account — a code. Same shape as the OAuth authorize
form (services/oauth/authorize_page.py), sharing its page shell.
"""

from html import escape
from typing import ClassVar

from services.auth.html_page import HtmlPage
from services.auth.passkeys.browser_script import PasskeyBrowserScript


class DocsLoginPageRenderer:
    # `credential`/`loginToken` are in scope where PasskeyBrowserScript
    # splices this in. A `fetch` rather than a form submission — unlike the
    # OAuth authorize page, there is no consent decision that has to survive
    # a redirect here, so the login can finish itself and navigate on success.
    _PASSKEY_SUBMIT: ClassVar[str] = """\
      var resp = await fetch("/docs/login/passkey", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          login_token: loginToken,
          credential: credential,
          next: nextEl ? nextEl.value : "/docs",
        }),
      });
      if (resp.status === 204) {
        location.assign(nextEl ? nextEl.value : "/docs");
      } else {
        throw new Error("Sign-in failed.");
      }
"""

    @classmethod
    def render_login_form(
        cls, *, next_path: str, passkeys_enabled: bool = False, error: str | None = None
    ) -> str:
        error_html = f'<p class="error">{escape(error)}</p>' if error else ""
        # Outside the <form>: the `required` attributes on username and
        # password would otherwise block a click on this button before any
        # JS ever runs, since it is not the field the browser is validating.
        passkey_html = (
            """
    <div class="actions">
      <button type="button" id="passkey">Sign in with a passkey</button>
    </div>
    """
            if passkeys_enabled
            else ""
        )
        body = f"""
    <h1>Sign in</h1>
    {error_html}
    <form method="post">
      <input type="hidden" name="next" value="{escape(next_path)}">
      <label>Username<input type="text" name="username" required autofocus
             autocomplete="username"></label>
      <label>Password<input type="password" name="password" required
             autocomplete="current-password"></label>
      <div class="actions">
        <button type="submit">Sign in</button>
      </div>
    </form>
    {passkey_html}
    """
        script = (
            PasskeyBrowserScript.render(
                options_url="/docs/login/passkey/options", submit=cls._PASSKEY_SUBMIT
            )
            if passkeys_enabled
            else ""
        )
        return HtmlPage.render(title="Documentation login", body=body, script=script)

    @classmethod
    def render_mfa_form(
        cls, *, next_path: str, mfa_token: str, error: str | None = None
    ) -> str:
        error_html = f'<p class="error">{escape(error)}</p>' if error else ""
        body = f"""
    <h1>Enter your code</h1>
    {error_html}
    <form method="post">
      <input type="hidden" name="next" value="{escape(next_path)}">
      <input type="hidden" name="mfa_token" value="{escape(mfa_token)}">
      <label>Code<input type="text" name="code" required autofocus
             inputmode="numeric" autocomplete="one-time-code"></label>
      <div class="actions">
        <button type="submit">Continue</button>
      </div>
    </form>
    """
        return HtmlPage.render(title="Documentation login", body=body)
