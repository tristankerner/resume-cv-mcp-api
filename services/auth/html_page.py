"""The minimal page shell shared by the two HTML surfaces this service
serves: the OAuth authorize form (services/oauth/authorize_page.py) and the
docs login (services/auth/docs_login_page.py).

Moved out of services/oauth/authorize_page.py rather than copied. The
authorize page's exact rendering is exercised by tests/test_oauth.py, so the
template string below must stay character-identical to what that file held —
this is a relocation, not a redesign.
"""

from typing import ClassVar


class HtmlPage:
    _TEMPLATE: ClassVar[str] = """\
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
    def render(cls, *, title: str, body: str) -> str:
        return cls._TEMPLATE.format(title=title, body=body)
