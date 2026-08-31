"""The two forms in front of the production documentation: a password, then
— for an MFA-enrolled account — a code. Same shape as the OAuth authorize
form (services/oauth/authorize_page.py), sharing its page shell.
"""

from html import escape

from services.auth.html_page import HtmlPage


class DocsLoginPageRenderer:
    @classmethod
    def render_login_form(cls, *, next_path: str, error: str | None = None) -> str:
        error_html = f'<p class="error">{escape(error)}</p>' if error else ""
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
    """
        return HtmlPage.render(title="Documentation login", body=body)

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
