# OAuth setup — operator steps

Connecting an MCP client to this service over OAuth 2.1: the settings to get
right, registering a client, and making the connection.

For the static-API-key alternative — simpler for scripts, and the only option
for a client that cannot do OAuth — see [mcp-clients.md](mcp-clients.md). Both
work; neither replaces the other.

`mcp.example.com` stands in for the real hostname throughout. How the settings
below reach the process is your deployment's business; this repository only
cares that they arrive.

**There is no self-service registration.** Anonymous Dynamic Client
Registration is off by default, so a client cannot create its own credentials —
you issue them in §6 and paste them into the client in §7. Registration writes
to the database, so it comes after the deploy rather than before.

| | Step |
| --- | --- |
| Before deploying | §1 `PUBLIC_BASE_URL` · §2 `OAUTH_ALLOWED_REDIRECT_HOSTS` · §3 `OAUTH_REGISTRATION_ENABLED` · §4 rate limiting |
| Deploy | §5 |
| After deploying | §6 register a client · §7 connect it · §8 verify · §9 tidy up |

---

## 1. Set `PUBLIC_BASE_URL`

The authorization server has to know its own issuer URL, and the resource
server its own resource URL. Neither can be derived from a request header a
caller controls, so it is configuration.

```
PUBLIC_BASE_URL=https://mcp.example.com
```

No scheme-less or trailing-slash forms — `https://mcp.example.com` exactly. It
is compared against the `aud` claim of every OAuth access token, so a mismatch
rejects every token with no useful error. It is a public hostname, not a
secret, so it needs no secret-handling on the way in.

## 2. Set `OAUTH_ALLOWED_REDIRECT_HOSTS`

This decides where an authorization code may be delivered. It is checked when
you register a client **and again at every authorization**, so it is live
policy rather than a one-time gate.

A comma-separated list taking as many hosts as you need:

```
OAUTH_ALLOWED_REDIRECT_HOSTS=claude.ai
OAUTH_ALLOWED_REDIRECT_HOSTS=claude.ai,chatgpt.com
OAUTH_ALLOWED_REDIRECT_HOSTS=claude.ai, chatgpt.com, *.staging.example.com
```

Surrounding whitespace and a trailing comma are ignored, and matching is
case-insensitive, so the third form is fine.

`claude.ai` alone covers Claude web, Desktop, mobile and Cowork, which all use
`https://claude.ai/api/mcp/auth_callback`. Claude Code uses a loopback address;
loopback is a built-in rule and must **not** be listed here.

Entries are **hosts, not URLs**. A bad entry fails startup rather than being
silently ignored, so a mistake fails the deploy instead of the connection:

| Wrong | Right |
| --- | --- |
| `https://claude.ai/api/mcp/auth_callback` | `claude.ai` |
| `claude.ai:443` | `claude.ai` |
| `*` or `*.com` | name the host |
| `localhost` | omit it — loopback is built in |

A `*.` wildcard covers subdomains **but not the apex**: `*.example.com` allows
`a.example.com` and not `example.com`. List both if you need both.

Keep the list short. Each entry is a host you are trusting not to have an open
redirect, since one would hand an attacker a code that lands on an allowed
origin.

**Removing a host takes effect immediately**, including for clients registered
while it was allowed — revoking trust in a host should stop it now, not at the
next token expiry.

## 3. Leave `OAUTH_REGISTRATION_ENABLED` alone

It defaults to `false` and should stay there — more so now than before:
`POST /oauth-clients` (§6) gives an admin an authenticated way to register a
client over HTTP, so there is no longer even the convenience argument for
leaving anonymous registration open. `POST /oauth/register` cannot ask who is
calling — a client registers before any user is involved — so open, it is an
unauthenticated database write anyone who finds it can repeat. The allowlist
in §2 bounds *where a code goes*; it does nothing about how many client rows a
stranger can create.

Closed, `POST /oauth/register` returns 404 and `registration_endpoint` is
absent from the authorization server metadata, which is how a conforming client
knows to expect credentials rather than to register itself.

**Only turn it on** for a client that speaks nothing but DCR: set it `true`,
deploy, connect that client, set it back to `false`, deploy again.

## 4. Rate-limit the token endpoint

`/oauth/token` is unauthenticated by necessity, the same as `/token`, and
deserves the same protection: a rate limit on whatever reverse proxy or CDN
sits in front of this service, keyed on IP address. The application's own
per-account and per-address lockout (see the README's "Failed logins are
throttled") is a backstop against a sustained attack, not a substitute — it
does not stop the requests from arriving in the first place.

No rule is needed for `/oauth/register` while §3 is `false` — the route 404s
before touching the database. If you ever turn it on, rate-limit it more
strictly than `/oauth/token`: every request that reaches it is a database
write.

Leave `/oauth/authorize` and `/resume/mcp` unlimited. The former is
interactive — a human retyping a password behind a shared address should not
be blocked at the edge, and it already routes password checks through the
account/address lockout above. The latter is authenticated by API key or OAuth
token and a tailoring run against it is legitimately bursty.

Whatever caches responses in front of this service should not cache
`/oauth/*` at all — caching an authorization response would serve one user's
redirect to another.

## 5. Deploy

Nothing OAuth-specific to do here beyond getting the three settings above into
the running process — see [running.md](running.md).

Migrations create the OAuth tables (`oauth_clients`,
`oauth_authorization_codes`, `oauth_refresh_tokens`). §6 writes to one of them,
which is why it comes after this rather than before.

## 6. Register a client

Registration is a deliberate act performed by an admin, over the API:

```bash
curl -sS -X POST https://mcp.example.com/oauth-clients \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}'
```

`$ADMIN_TOKEN` has to come from a password login (`POST /token`), not an API
key: `POST /oauth-clients` requires `users:admin` **and** an interactive
login, the same reasoning `DELETE /users/{id}/mfa` uses — an admin key that
could mint an OAuth client could mint itself a fresh path into the service.
If a browser client is in front of this deployment, it has an admin screen
for the same operation; see the README's "Administration" for the full
surface and why each route does or does not accept a key.

The response carries the new client alongside a `client_secret` — **shown
once**; only its hash is stored. Register again to issue a replacement if you
lose it.

| Field | Effect |
| --- | --- |
| `"public": true` | no secret; PKCE alone binds the exchange. For a client that cannot keep one. |
| `"scope"` | space-delimited default scopes |

`GET /oauth-clients` lists what is registered (also admin-only, also
interactive), and `DELETE /oauth-clients/{client_id}` deregisters one,
invalidating every authorization code and refresh token it holds in the same
request.

The redirect URI is checked against `OAUTH_ALLOWED_REDIRECT_HOSTS` exactly as
an HTTP registration would be, so a typo is refused here — 400, naming the
rejected URI — rather than confusingly later at authorize time.

### Claude Code needs a pinned port

Its redirect is a loopback address on an ephemeral port, and redirect URIs are
matched exactly. Pick a port, register it, and pass the same one:

```bash
curl -sS -X POST https://mcp.example.com/oauth-clients \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"client_name": "Claude Code", "redirect_uris": ["http://127.0.0.1:41703/callback"], "public": true}'
```

Then add `--callback-port 41703` to the `claude mcp add` command in §7.

## 7. Connect the client

Point the client at:

```
https://mcp.example.com/resume/mcp
```

**claude.ai, Desktop, mobile, Cowork** — add a custom connector, then put the
`client_id` and `client_secret` from §6 into **Advanced settings**. There is no
field for an API key, and there should not be. Because the metadata advertises
no `registration_endpoint`, the connector knows to use the credentials you
supplied instead of minting its own.

**Claude Code**:

```bash
claude mcp add --transport http --scope user --client-id YOUR_CLIENT_ID --client-secret --callback-port 41703 resume https://mcp.example.com/resume/mcp
```

`--client-secret` prompts rather than taking the value on the command line, so
it stays out of your shell history. Omit it for a `--public` client.

Either way the client then performs discovery and sends you to
`/oauth/authorize` to log in and approve. **Approve only the scopes that client
needs**, and log in as the **account that owns the documents** — reads are
scoped to the owner, so approving as anyone else yields a connector that
authenticates successfully and sees an empty store.

If that account has a second factor enrolled, a code prompt follows the
password page before the redirect — the same challenge `/token` uses, in the
same shape, with the connector's protocol parameters carried forward in
hidden fields so nothing about the authorization request is lost across the
extra round trip.

## 8. Verify

Run these in order and stop at the first failure; each depends on the one
before, and a client fails silently where these fail loudly.

**The 401 must point somewhere:**

```bash
curl -sS -o /dev/null -D - -X POST https://mcp.example.com/resume/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{}' | grep -i 'www-authenticate'
```

Expect `resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/resume/mcp"`.
A bare `Bearer` means the resource server is not advertising itself.

**The protected-resource document must resolve at the origin root:**

```bash
curl -sS https://mcp.example.com/.well-known/oauth-protected-resource/resume/mcp | jq
```

Expect `resource` to be `https://mcp.example.com/resume/mcp`. If this 404s but
`/resume/.well-known/…` works, the well-known routes were not re-registered on
the root app — a code bug, not a setup one.

**The authorization server must describe itself:**

```bash
curl -sS https://mcp.example.com/.well-known/oauth-authorization-server | jq
```

Expect `authorization_endpoint`, `token_endpoint`, `revocation_endpoint`, and
`code_challenge_methods_supported: ["S256"]`.

**`registration_endpoint` must be absent.** Its presence means §3 is on when
you did not intend it. Note that `issuer` carries a trailing slash and matches
`authorization_servers` in the previous document byte for byte; that is
deliberate, and a client will reject the document if they ever differ.

**`/token` must still be the password login**, not the OAuth token endpoint:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://mcp.example.com/token -d 'username=x&password=y'
```

Expect `401`, the ordinary wrong-password answer. A `400` with an
`{"error": ...}` body would mean the OAuth token endpoint was mounted over it.

**Static API keys still work alongside OAuth** — enabling one does not disable
the other. See [mcp-clients.md](mcp-clients.md).

## 9. After it works

**Revoke the API key you were using for MCP**, if the connector has replaced
it. An unused credential holding document read scopes is surface for no
benefit:

```bash
curl -sS -X DELETE https://mcp.example.com/api-keys/KEY_ID -H "Authorization: Bearer $TOKEN"
```

**Check what is registered** and confirm it is only what you created:

```bash
curl -sS https://mcp.example.com/oauth-clients -H "Authorization: Bearer $ADMIN_TOKEN"
```

With §3 off there is no path by which anything else could appear, so an
unfamiliar entry means registration was open at some point — check that it is
`false` and redeploy.

**Revoking a client**:

```bash
curl -sS -X DELETE https://mcp.example.com/oauth-clients/THE_CLIENT_ID \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

This removes every authorization code and refresh token the client holds in
the same request — `oauth_refresh_tokens.client_id` and
`oauth_authorization_codes.client_id` are foreign keys onto
`oauth_clients.client_id`, and the service deletes the dependants first rather
than leaving that to the database. Both `/oauth/authorize` and `/oauth/token`
then refuse the client immediately — each loads the client row first and
answers `invalid_client` when it is gone.

Its **access tokens survive until they expire**, because they are stateless
JWTs that no lookup validates against the client. That window is
`AUTH_ACCESS_TOKEN_EXPIRE_MINUTES`, 30 by default. If you need someone cut off
sooner than that, deactivate the user instead — every request re-reads the user
record, so that takes effect on the next call.
