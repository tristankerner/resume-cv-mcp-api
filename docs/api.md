# API Reference

Every endpoint, generated from the service's own OpenAPI schema. The live,
interactive version is served at `/docs` and `/redoc` — open in development,
behind a login in production.

**Auth column:** _none_ takes no credential · _key_ accepts an API key or a
JWT · _login_ requires an interactive password JWT and refuses API keys.

## Authentication

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/token` | none | Password login. Form-encoded `username`, `password`. |
| `POST` | `/token/mfa` | none | Redeem an MFA challenge. Form-encoded `mfa_token`, `code`. |

`POST /token` returns one of two shapes. Without a second factor enrolled:

```json
{"access_token": "…", "token_type": "bearer"}
```

With one, a challenge to redeem at `/token/mfa` — so a script that reads
`.access_token` straight off this response gets `null` for an enrolled account:

```json
{"mfa_required": true, "mfa_token": "…", "methods": ["totp"], "expires_in": 300}
```

`/token/mfa` returns the first shape. Both paths issue the same kind of token,
so either satisfies the interactive-login requirements below.

## Documents

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/documents` | any read scope | List your documents — name, type, public flag, latest revision and note. No content. |
| `GET` | `/documents/schemas` | any read scope | JSON Schema for each type you may read. |
| `GET` | `/documents/resume/{name}` | `resume:read` | Read a résumé. `?revisions=1..50&order=newest_first\|oldest_first` |
| `GET` | `/documents/metadata/{name}` | `metadata:read` | Read a metadata document. Same query parameters. |
| `GET` | `/documents/skill/{name}` | `skill:read` | Read a skill document. Same query parameters. |
| `POST` | `/documents/resume` | `resume:write` | Create or append a revision. Body: `name`, `data`, optional `public`, `revision_note`. |
| `POST` | `/documents/metadata` | `metadata:write` | As above, typed `metadata`. |
| `POST` | `/documents/skill` | `skill:write` | As above, typed `skill`. |
| `PATCH` | `/documents/{name}` | stored type's **write + delete** | Rename. Writes rows under the new name and removes the old, hence both scopes. |
| `DELETE` | `/documents/{name}` | stored type's **delete** | Delete a document and all its revisions. |

Reads always return `{"data": [ ... ]}`. Writes return
`{"name", "revision_id", "status"}`, where `status` is `"created"` or
`"unchanged"`. See [Document Types](document-types.md).

## Public feed

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/public/{username}/resume/{name}` | none | The redacted public projection of a `public` résumé. |
| `GET` | `/public/users/{user_id}/resume/{name}` | none | The same, addressed by user id. |

Both answer with `Access-Control-Allow-Origin: *`. A private document is a
`404`, never a `403` — whether one exists isn't disclosed.

## API keys

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api-keys` | **login** | Mint a key. Body: `name`, `scopes`. The secret is in the response and not recoverable afterwards. |
| `GET` | `/api-keys` | key | List your keys — metadata only, never the secret. |
| `DELETE` | `/api-keys/{key_id}` | **login** | Revoke. Soft delete, so `last_used_at` stays readable. |

Self-service only, on your own keys. A key can read the list but can't mint or
revoke — so a leaked key cannot issue its own replacement ahead of revocation.

Requested scopes are capped at the owner's current role scopes, reloaded at
mint time rather than taken from the token.

## Users

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/users/me` | key | Your own account. |
| `POST` | `/users/me/password` | **login** | Change your own password. Requires the current one. |
| `PATCH` | `/users/{user_id}` | key, or **login** for recovery fields | Edit an account. `password`, `username`, `email` and `roles` require an interactive login. |
| `GET` | `/users` | key + `users:admin` | List every user. |
| `POST` | `/users` | **login** + `users:admin` | Create an account. Seeded with the three example documents. |
| `POST` | `/users/{user_id}/password` | **login** + `users:admin` | Admin password reset, bypassing the current one. |
| `DELETE` | `/users/{user_id}/lock` | key + `users:admin` | End a login lockout. |
| `DELETE` | `/users/{user_id}/mfa` | **login** + `users:admin` | Strip every second factor from an account. |

Passwords must be 8+ characters with upper, lower, digit and symbol.

## Multi-factor authentication

All self-service, on your own account, and **all require an interactive
login** — an API key cannot read or change second factors, the same rule key
management follows.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/users/me/mfa` | **login** | Enrolled methods and their status. |
| `POST` | `/users/me/mfa/totp` | **login** | Begin TOTP enrolment. Returns the secret and provisioning URI. |
| `POST` | `/users/me/mfa/totp/{credential_id}/activate` | **login** | Confirm enrolment with a code from the app. |
| `POST` | `/users/me/mfa/backup-codes` | **login** | Generate a set of single-use codes, replacing any previous set. |
| `DELETE` | `/users/me/mfa/{credential_id}` | **login** | Remove one method. Body carries the current password. |

## OAuth 2.1

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/.well-known/oauth-authorization-server` | none | RFC 8414 metadata. `registration_endpoint` is absent when registration is closed. |
| `GET` | `/.well-known/oauth-protected-resource/resume/mcp` | none | RFC 9728 resource metadata, pointing at the authorization server. |
| `GET` | `/oauth/authorize` | none | Login and consent page. |
| `POST` | `/oauth/authorize` | none | Submit login and consent; redirects with a code. |
| `POST` | `/oauth/token` | client credentials | Exchange a code, or refresh. PKCE `S256` required. |
| `POST` | `/oauth/revoke` | client credentials | Revoke a refresh token. |
| `POST` | `/oauth/register` | none | Dynamic Client Registration. `404` unless `OAUTH_REGISTRATION_ENABLED=true`. |

### Client administration

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/oauth-clients` | **login** + `users:admin` | List registered clients. |
| `POST` | `/oauth-clients` | **login** + `users:admin` | Register a client. The secret is shown once. |
| `DELETE` | `/oauth-clients/{client_id}` | **login** + `users:admin` | Deregister, invalidating every code and refresh token it holds. |

Redirect URIs are checked against `OAUTH_ALLOWED_REDIRECT_HOSTS` at
registration **and again at every authorization**, so it's live policy rather
than a one-time gate.

## MCP

| Path | Transport | Auth |
| --- | --- | --- |
| `/resume/mcp` | Streamable HTTP | API key or OAuth token |

| Tool | Scope | Returns |
| --- | --- | --- |
| `list_resume_documents` | any of the three read scopes | Your documents — id, type, public, revision, note, created_at. No content. Only types the credential may read. |
| `retrieve_resume_data` | `resume:read`, plus `metadata:read` / `skill:read` when those ids are passed | All three documents in one call. A companion that isn't stored comes back `null`. |

Both tools return a single text content block with no `structuredContent`
mirror, which halves the token cost of a call.

## Other

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/` | none | Service name and version. |
| `GET` | `/client` | none | The browser client, when `CLIENT_HTML_PATH` is set. |
| `GET` | `/docs`, `/redoc`, `/openapi.json` | none, or docs login in production | Interactive API documentation. |

## Status codes

| Code | Means |
| --- | --- |
| `401` | No credential, a bad one, or a permanently locked account |
| `403` | Authenticated, but the scope is missing |
| `404` | Not found — also what a private document returns anonymously |
| `409` | A write whose type disagrees with the stored document's type |
| `422` | Payload failed validation, or a document name that can't appear in a URL path |
| `429` | Login throttled; `Retry-After` says how long |

## Regenerating this reference

The schema this page describes comes from the running application:

```bash
curl -s localhost:8000/openapi.json | jq '.paths | keys'
```
