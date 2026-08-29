"""The one control on where an authorization code can be delivered.

Registration under RFC 7591 is deliberately credential-free — an MCP client
registers before any user is involved — so nothing about *who* is registering
can be checked. What is checked instead is *where the code goes*: the redirect
URI is the only field in a registration request that must be real, because it
is where the value that matters is sent.

Matched structurally, never as a string — see the hostile-input table in the
plan. `urlsplit` is what makes `user@host` and `host.evil.com` fail the way
they should, rather than the way a regex would.
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlsplit


class RedirectAllowlist:
    # Loopback is a built-in rule, not a configurable entry: RFC 8252 §7.3
    # requires an authorization server to permit a variable port for a native
    # client's loopback redirect, and a config entry could be widened by a typo.
    LOOPBACK_HOSTS: ClassVar[frozenset[str]] = frozenset(
        {"127.0.0.1", "::1", "localhost"}
    )

    def __init__(self, hosts: frozenset[str]):
        self.hosts = hosts

    @staticmethod
    def _normalize_host(host: str) -> str | None:
        """Lowercase and punycode-encode, or None if it cannot be a host at all."""
        try:
            return host.lower().encode("idna").decode("ascii")
        except UnicodeError:
            return None

    @classmethod
    def _validate_and_normalize_entry(cls, entry: str) -> str:
        """One `OAUTH_ALLOWED_REDIRECT_HOSTS` entry -> its normalized form.

        Raises ValueError on anything that is not a bare host, so a misconfigured
        deployment fails at startup instead of silently blocking every
        registration (an empty allowlist) or accepting something wider than
        intended (a URL, a port, a public suffix).
        """
        lowered = entry.lower()
        if "://" in lowered or "/" in lowered:
            raise ValueError(f"{entry!r} is a URL, not a host")
        if "@" in lowered:
            raise ValueError(f"{entry!r} contains userinfo, which has no meaning here")
        if lowered in cls.LOOPBACK_HOSTS:
            raise ValueError(
                f"{entry!r} is a loopback host; loopback is a built-in rule, not "
                "configurable"
            )

        wildcard = lowered.startswith("*.")
        label = lowered[2:] if wildcard else lowered
        if "*" in label:
            raise ValueError(
                f"{entry!r} may carry at most one leading '*.' and no other '*'"
            )
        if not label:
            raise ValueError(f"{entry!r} has no host after the wildcard")
        if ":" in label:
            raise ValueError(
                f"{entry!r} contains a port, which is not part of the match"
            )
        if wildcard and "." not in label:
            raise ValueError(
                f"{entry!r} is a public suffix — a wildcard over an entire TLD"
            )

        encoded = cls._normalize_host(label)
        if encoded is None:
            raise ValueError(f"{entry!r} is not a valid hostname")
        return f"*.{encoded}" if wildcard else encoded

    @classmethod
    def parse(cls, raw: str | None) -> RedirectAllowlist:
        """`OAUTH_ALLOWED_REDIRECT_HOSTS` -> the allowlist DCR may register against.

        An empty or unset value is a configuration error, not an empty allowlist:
        the latter would silently block every registration and read like a server
        fault weeks later. Whitespace around an entry and a trailing comma are
        tolerated; everything else invalid fails loudly — see
        `_validate_and_normalize_entry` for exactly what is refused and why.
        """
        entries = [part.strip() for part in (raw or "").split(",")]
        entries = [entry for entry in entries if entry]
        if not entries:
            raise ValueError(
                "OAUTH_ALLOWED_REDIRECT_HOSTS must not be empty — an empty "
                "allowlist blocks every client registration"
            )
        return cls(
            frozenset(cls._validate_and_normalize_entry(entry) for entry in entries)
        )

    @classmethod
    def is_loopback_host(cls, host: str) -> bool:
        return host.lower() in cls.LOOPBACK_HOSTS

    def _host_allowed(self, host: str) -> bool:
        normalized = self._normalize_host(host)
        if normalized is None:
            return False
        if normalized in self.hosts:
            return True
        for entry in self.hosts:
            if not entry.startswith("*."):
                continue
            suffix = entry[1:]  # ".example.com" — keeps the leading dot
            # `endswith` is what keeps a wildcard from matching its own apex:
            # "example.com" is shorter than ".example.com" and so cannot end
            # with it. The length comparison covers the one case `endswith`
            # lets through — a host equal to the suffix itself, ".example.com",
            # since every string ends with itself. `_normalize_host` already
            # rejects that above (a leading empty label is not encodable), so
            # this is defence in depth rather than the only thing standing in
            # the way.
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                return True
        return False

    def allows(self, uri: str) -> bool:
        """Whether `uri` may be registered or authorized against as a redirect.

        Loopback hosts are always allowed, on any port, over http — the built-in
        rule RFC 8252 requires for native clients. Everything else must be https
        and structurally match an entry in `self.hosts`.
        """
        try:
            parsed = urlsplit(uri)
        except ValueError:
            return False

        # RFC 6749 §3.1.2: a redirection endpoint URI MUST NOT include a
        # fragment component. Tested on the raw string rather than
        # `parsed.fragment`, which reports "" for both ".../cb" and ".../cb#"
        # and so cannot tell an empty fragment from none at all. An unencoded
        # "#" is always the fragment delimiter — one meant literally would be
        # percent-encoded — so this rejects exactly the URIs that carry a
        # fragment.
        if "#" in uri:
            return False

        host = parsed.hostname
        if not host:
            return False
        # `https://claude.ai@evil.com/cb` parses to hostname="evil.com" with
        # username="claude.ai" — rejecting on userinfo at all catches this
        # regardless of what the real host turns out to be.
        if parsed.username is not None or parsed.password is not None:
            return False

        if self.is_loopback_host(host):
            return parsed.scheme in ("http", "https")

        if parsed.scheme != "https":
            return False
        return self._host_allowed(host)
