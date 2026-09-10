from typing import ClassVar


class PasskeyBrowserScript:
    """The vanilla-JS half of a ceremony, for the two server-rendered login
    pages.

    The SPA uses @simplewebauthn/browser; these pages have no build step, so
    the base64url marshalling `navigator.credentials` needs is written out
    here. Kept in one module and shared by both pages rather than pasted into
    each: it is the only JavaScript this service ships, and one copy is one
    thing to get right.

    `.replace()` on unique tokens rather than `str.format()`: the body is
    JavaScript, whose own `{`/`}` would otherwise all need escaping.
    """

    # A single generic body-builder reads whichever of these hidden fields the
    # page happens to have — `next` on the docs login, `client_id` on the
    # OAuth authorize form — so `render` needs no per-surface parameter for
    # what the options POST carries.
    _TEMPLATE: ClassVar[str] = """\
<script>
(function () {
  var btn = document.getElementById("passkey");
  if (!btn) return;
  if (!(window.PublicKeyCredential && window.isSecureContext)) {
    btn.remove();
    return;
  }

  var b64uToBytes = function (s) {
    var b64 = s.replace(/-/g, "+").replace(/_/g, "/")
      .padEnd(Math.ceil(s.length / 4) * 4, "=");
    return Uint8Array.from(atob(b64), function (c) { return c.charCodeAt(0); });
  };
  var bytesToB64u = function (b) {
    return btoa(String.fromCharCode.apply(null, new Uint8Array(b)))
      .replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, "");
  };

  var showError = function (message) {
    var errorEl = document.querySelector(".error");
    if (!errorEl) {
      errorEl = document.createElement("p");
      errorEl.className = "error";
      btn.insertAdjacentElement("afterend", errorEl);
    }
    errorEl.textContent = message;
  };

  btn.addEventListener("click", async function () {
    try {
      var usernameEl = document.querySelector('input[name="username"]');
      var nextEl = document.querySelector('input[name="next"]');
      var clientIdEl = document.querySelector('input[name="client_id"]');
      var body = {};
      if (usernameEl && usernameEl.value) body.username = usernameEl.value;
      if (nextEl) body.next = nextEl.value;
      if (clientIdEl) body.client_id = clientIdEl.value;

      var optionsResponse = await fetch("__OPTIONS_URL__", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!optionsResponse.ok) {
        throw new Error("Could not start the passkey sign-in.");
      }
      var optionsBody = await optionsResponse.json();
      var options = optionsBody.options;
      var loginToken = optionsBody.login_token;

      options.challenge = b64uToBytes(options.challenge);
      if (options.allowCredentials) {
        options.allowCredentials = options.allowCredentials.map(function (c) {
          return Object.assign({}, c, { id: b64uToBytes(c.id) });
        });
      }

      var assertion = await navigator.credentials.get({ publicKey: options });
      var credential = {
        id: assertion.id,
        rawId: bytesToB64u(assertion.rawId),
        type: assertion.type,
        response: {
          clientDataJSON: bytesToB64u(assertion.response.clientDataJSON),
          authenticatorData: bytesToB64u(assertion.response.authenticatorData),
          signature: bytesToB64u(assertion.response.signature),
          userHandle: assertion.response.userHandle
            ? bytesToB64u(assertion.response.userHandle)
            : null,
        },
        clientExtensionResults: {},
      };

      __SUBMIT__
    } catch (err) {
      showError(err.name === "NotAllowedError" ? "Cancelled." : (err.message || "Passkey sign-in failed."));
    }
  });
})();
</script>
"""

    @classmethod
    def render(cls, *, options_url: str, submit: str) -> str:
        """A <script> block. `options_url` is the route that mints options;
        `submit` is a JS statement run with `credential` and `loginToken` in
        scope, naming what to do with the assertion — see the two callers.
        """
        return cls._TEMPLATE.replace("__OPTIONS_URL__", options_url).replace(
            "__SUBMIT__", submit
        )
