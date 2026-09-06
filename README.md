# resume-cv-mcp-api

A résumé/CV — not anything about resuming a process — served as structured
data over HTTP and MCP.

Used to store and serve JSON résumés, résumé metadata (also JSON), and 
fine-tuning skills as JSON. A user may have multiples of these documents. These
document is server via a both public (optional) and private REST endpoints, as 
well as via MCP tools.

Documents may optionally be marked as public. Additionally, sections within a résumé
may be hidden from the public feed.

## Author's Note

**Q:** What problem(s) does this solve?

**A:** This micro-ish service enables me to:
- Have a single source of truth for my résumé in a structure format.
- Track changes/revisions to my résumé.
- Feed into my personal website, which can also compile this structured 
resume into a master resume docx file (minus personal contact info).
- Have a centralized place to store and track resume metadata, and 
fine-tuning-skill prompts.
- Run `/fine-tune-resume` in Claude (or potentially others) from anywhere, on 
any device that supports MCP, along with any job description, and receive a 
pretty good fine-tuned-resume and cover letter to use for a recruiter or job 
application.
- Potentially let other friends use this for the same purpose.

**Q:** Why does this exist?

**A:** It's 2026, and I've been job hunting for a little while now. I've found myself 
frustrated by multiple activities that I'm intended to repeat, day in, and day out.
This includes:
- An expectation to fine tune my résumé for every job, for every recruiter. Keywords 
must match (even if it is the most mundane nonsensical thing you've ever heard of.)
- Fine-tuning must be done quickly, or expect a call back asking for when it's done.
And then another call. Whether you happen to be at your computer or not.
- Regularly having to update my résumé with new history or skills, because I got a new 
cert, or just never thought hat particularly thing was worth mentioning. Then losing 
that update, because I made it in a fine-tuned resume, and not in a master.
- Updating the public master resume, private master resume, and website, every time. 

**Q:** Why this way?

**A:** https://xkcd.com/1319/ 

**Q:** Where's the gui?

**A:** There is a separate repo, public alongside this one:
[resume-cv-mcp-api-web-client](https://github.com/tristankerner/resume-cv-mcp-api-web-client)

At the end of the day, I don't know that this project will be particularly useful for 
anyone else. For the most part, it was something I wanted, for my workflow, and a way 
to practice using Claude Code with a stack that I'm already familiar with. It was also 
a chance to dive into MCP (using FastMCP), and work with skills.

There is almost certainly a simpler way to do what I/"we" have done here. Also, an 
easier way. Certainly better ways if your intent is to mass-apply to jobs, instead
of being as selective as I'm being.

I also can't really say how well this works... it certainly does what it's intended
to do. I like the results of both the résumé and cover letter compared to some other
manual approached. However, I also haven't gotten a job with it yet, which is the 
real point.

## Setup

### Without the web client

```bash
cp .env.example .env   # then fill in AUTH_SECRET_KEY at minimum
docker compose up --build
```

Reachable at `http://localhost:8000`; `/docs` is open since `ENVIRONMENT`
defaults to `development`. The named `resume-data` volume is intentional, not
a shortcut — see [docs/running.md](docs/running.md)'s "Docker" section for why
a bind mount does not work here (the image runs as a non-root user).
`GET /client` is not registered on this path — see below to change that, or
[docs/local-only.md](docs/local-only.md) for the fully offline native-Python
walkthrough instead of Docker.

### With the web client

```bash
docker compose build
cp .env.example .env   # then fill in AUTH_SECRET_KEY at minimum
docker compose -f docker-compose.web-client.yaml up --build
```

Then open `http://localhost:8000/client` and log in with the bootstrap admin
credentials from `.env`. This builds
[`resume-cv-mcp-api-web-client`](https://github.com/tristankerner/resume-cv-mcp-api-web-client)
from source — `main` by default, override with `CLIENT_REF=<sha-or-tag>` —
and bakes it into the image at `CLIENT_HTML_PATH`, same-origin, so
`CLIENT_ALLOWED_ORIGINS` does not need to name anything. See
`docker-compose.web-client.yaml`'s own comments for why the plain build has to
run first: Compose builds each service from its own Dockerfile, and cannot
chain one service's output into another's build context as a dependency.

Developing against a local client checkout instead of a pinned commit needs a
manual build — see "Browser client" below.

The rest of this README goes deep on how documents, authorization, and the
client all fit together.

---

## Documents

| Type | Model | Holds |
| --- | --- | --- |
| `resume` | `ResumePrivate` | A JSON Resume 1.0 document (https://jsonresume.org/schema), extended with a few private fields: what happened (work history, bullets, per-bullet tech and figures, skill ratings), contact details, narrative material. Individual entries can be withheld from the public feed with `publish: false` — see "The website" below |
| `metadata` | `ResumeMetadata` | What each field means, which parts are resume-ready, how the highlight ids join, what must never be published |
| `skill` | `ResumeSkill` | What to do with the other two: the persona, the procedure, the selection and wording rules, the cover-letter guidance, the guardrails, one output spec per artifact |

A document is `(owner, name)`; the name is the operator's choice, and its
`type` is fixed at creation — a write that disagrees with a document's stored
type is a 409, not a silent retype. The MCP `retrieve_resume_data` tool takes
the three ids by name (discovered with `list_resume_documents`) and returns all
three in one call, so a tailoring run cannot pair current instructions with a
stale resume, or the reverse. Only the résumé is required; a companion that is
not stored comes back as `null` rather than failing the retrieval, so the
client can say what it is working without.

Both MCP tools return a single text content block by design, with no
`structuredContent` mirror, to keep the token cost of a call to half of what a
duplicated payload would otherwise cost. Each document also has its defaults
dropped on the way out, so an absent field means "the schema's default", not
"unset" — worth knowing if you write a client against the payload rather than
against the schema served at `GET /documents/schemas`.

Keeping the instructions in a document rather than in the client's skill file is
the point of the third type: editing them is a document write, and the next run
picks them up without a skill release. It also keeps them in one place when more
than one client asks for the same résumé.

Each type has its own write routes — `/documents/resume`, `/documents/metadata`,
`/documents/skill` — because a route per payload is what validates the shape on
the way in. Each route sets exactly one type on what it stores; the name is
free, short of the shapes that cannot be spelled as a URL path segment — blank,
over 255 characters, or containing a slash or a control character. Those are
refused with a 422, because a document whose name cannot appear in a path is
one nothing can read back or delete.

Filled-in documents are personal, so none are checked in here. `examples/`
holds small, fictional fixtures that validate against the three models above —
enough structure to see every field, none of it real.

**A new account starts with these three already in place.** `POST /users` and
application startup's admin bootstrap both seed a private (`public: false`)
`resume`, `metadata` and `skill` document from the `document_schema` catalogue
(below) on creation, so a fresh account is never empty — see
`services/user/document_seeder.py`. `examples/*.json` still exist and still
matter — they are what `examples/seed-documents.sh` loads, what
`tests/test_path_resolution.py` resolves paths against, and what the frozen
migration snapshots below were generated from — but a new account is seeded
from the catalogue, not from those files directly; `tests/test_catalogue_examples.py`
is what keeps the two from drifting apart.

### The schema catalogue

Every stored document revision carries a `schema_version`: the row in
`document_schema` — keyed `(version, document_type)` — it was written
against. `document_schema` is a historical record, populated only by
migrations, never by the application: each row freezes that version's JSON
Schema and a fictional example. `documents(type, schema_version)` carries a
real foreign key into it, so a row naming a version this build's catalogue
does not know about cannot be written.

This is what let the v1 (bespoke) to v2 (JSON Resume) resume conversion ship
as an ordinary deploy: two Alembic revisions convert every stored `resume`,
`metadata` and `skill` document in place, appending a new revision rather
than rewriting history, auditing every document before writing and aborting
the whole migration on any loss. See `SCHEMA_VERSIONING_PLAN.md` for the
full design and rollback story, and `scripts/migrate_resume_v2.py` for the
demoted preview tool the migrations' converter is a frozen copy of.

One consequence worth knowing if you read document history: a revision
written at a schema version this build no longer considers current is
served **unvalidated**, as the raw stored payload, rather than 500ing —
`GET /documents/resume/{name}?revisions=5` against an account whose history
spans the v1→v2 conversion returns both shapes side by side.

### Revisions

`GET /documents/{resume,metadata,skill}/{document_name}` always returns the
list envelope, `{"data": [ ... ]}` — one element by default. Two query
parameters widen it:

- `revisions` (default 1, max 50) — how many of the most recent revisions to
  return.
- `order` (`newest_first` or `oldest_first`) — how that slice is arranged.
  `revisions=3&order=oldest_first` is the three **most recent** revisions,
  oldest of those three first — not the three oldest revisions overall.

`GET /documents` lists the caller's own documents — name, type, whether it is
public, latest revision number and note — with no content, one row per
document.

### Publishing

A document is served anonymously only once `public` is set on it, and a write
that flips the flag records a new revision even when `data` is byte-identical —
publishing and unpublishing are edits, and the history should say when they
happened.

`public` is three-valued on the way in. Omitting it leaves the flag as the
current revision has it; a first write that omits it defaults closed. A plain
`false` default would mean any content edit that forgot to restate the flag
silently took the document offline, and the symptom — a website serving 404 —
points nowhere near the write that caused it.

## Authorization model

Authentication produces a **Principal**: a user id, a set of **scopes**, and
which kind of credential proved it. Every authorization decision reads scopes
off that object. Nothing compares usernames, and an unauthenticated request has
no Principal at all rather than a stand-in identity.

One scope per document type per verb, plus one for user administration:

| | read | write | delete |
| --- | --- | --- | --- |
| **resume** | `resume:read` | `resume:write` | `resume:delete` |
| **metadata** | `metadata:read` | `metadata:write` | `metadata:delete` |
| **skill** | `skill:read` | `skill:write` | `skill:delete` |

| Scope | Grants |
| --- | --- |
| `users:admin` | listing and creating users, resetting a password, clearing a lock, stripping MFA, and registering an OAuth client — see "Administration" below |

Split by type rather than one `resume:*` family covering all three, because
the three documents are not equally sensitive: the skill document is
procedure, the metadata is schema documentation, and the résumé is contact
details and per-bullet figures. A client that only follows the instructions
has no business holding a credential that reads the résumé.

There is no scope for the published projection. It takes no credential at all,
so a scope naming it would be checked nowhere while still being mintable onto a
key, where it would read as though it did something.

### Two roles

| Role | Holds |
| --- | --- |
| `admin` | every scope |
| `member` | every document scope; not `users:admin` |

A member owns their documents outright, so ownership rather than the role is
what keeps them out of anyone else's, and user administration is the only
thing left to withhold.

There is no `mcp` role. A document has an owner, so an MCP client
authenticates **as that owner** with an API key narrowed to the scopes it needs
— finer-grained than a role, revocable on its own, and without a second account
holding a copy of someone's résumé.

What each role grants lives in the `role_scopes` table (seeded by migration,
cached in memory for 60 seconds — see `services/auth/scopes.py`), not in code.
Scopes are derived from the user's roles on **every request**, so revoking a
role takes effect immediately rather than when a token expires.

### Two credential types

**JWT** — `POST /token` with a username and password, short-lived. For
interactive use and for anything that manages credentials. The token's subject
is the user id, so renaming an account does not log it out, and the token
carries nothing mutable — identity and permissions are resolved per request.

**API key** — long-lived, for machine clients. Presented the same way
(`Authorization: Bearer …`), distinguished by its `rsm_` prefix. A key's
effective scopes are `key.scopes ∩ owner's role scopes`, so a key can be
narrowed below its owner but never widened, and it dies when its owner is
deactivated or loses the role.

Keys are stored as SHA-256 of the secret, never in the clear. SHA-256 rather
than the password hasher on purpose: verification runs on every request, and
these are 32 random bytes with no dictionary to attack — a deliberately slow
KDF would only be a self-inflicted denial of service.

Keys are **strictly self-service**: `POST /api-keys`, `GET /api-keys` and
`DELETE /api-keys/{id}` all act on the caller's own keys only, with no
admin-on-behalf path. An admin managing someone else's account uses the
routes under "Administration" below instead.

**A key cannot manage keys.** Minting and revoking require an interactive
login, so a leaked key cannot issue its own replacement ahead of revocation.

**A key cannot touch its own account's recovery either.** `password`,
`username` and `email` on `PATCH /users/{id}` are account-recovery fields —
whoever controls them can lock out or take over the account — so a self-edit
touching any of them requires an interactive login too, the same rule and the
same reasoning as key management. Editing anything else (`first_name`,
`last_name`, `disabled`) is unaffected. Holding `users:admin` is not an
exemption: an admin key that could set another account's password could log
in as that account, which would make every other interactive-login rule here
decorative. `roles` is gated the same way, since promoting an account you can
already authenticate as reaches the same place by a different door.
Changing your own password has its own route,
`POST /users/me/password`, which additionally requires the *current* password
— `PATCH` never checks it.

### Administration

Every route below requires `users:admin`. Each also states whether an API key
holding that scope is enough, or whether it demands an interactive login (a
password JWT) — the same asymmetry `unlock_user` draws against `reset_mfa`
above, applied consistently: an operation a locked-out admin needs to reach
for *themselves* stays open to a key; one that could become a fresh way into
the service, or into someone else's account, does not.

| Route | API key | Why |
| --- | --- | --- |
| `GET /users` | yes | Read-only. |
| `POST /users` | no | Creating an account sets its password, so a key that could do it could mint an account — with `roles: ["admin"]`, a peer of the caller — and then log in as it. That is the row below, one step removed. |
| `POST /users/{id}/password` | no | An admin key that could set any password would own every account outright. `PATCH /users/{id}` is held to the same rule for the same reason; there is no back door through it. |
| `DELETE /users/{id}/mfa` | no | An admin key that could strip second factors would make MFA optional service-wide for whoever steals it. |
| `DELETE /users/{id}/lock` | **yes** | The documented escape hatch — a lock only blocks the password path, so a locked-out admin's own key must still be able to clear it. |
| `GET /oauth-clients` | no | Grouped with the other two below rather than treated as merely read-only: this is the same admin surface, and a key that can enumerate registered clients is most of the way to registering one. |
| `POST /oauth-clients` | no | An API key that can mint an OAuth client can mint itself a fresh path into the service. |
| `DELETE /oauth-clients/{id}` | no | Same reasoning as registering one. |

`GET /oauth-clients`, `POST /oauth-clients` and `DELETE /oauth-clients/{id}`
manage clients pre-registered for OAuth 2.1 connectors — see
[`docs/oauth-setup.md`](docs/oauth-setup.md). Registering one this way is the
same operation Dynamic Client Registration performs, gated behind
`users:admin` instead of being anonymous: it delegates to the same
redirect-allowlist check either way, so a URI outside
`OAUTH_ALLOWED_REDIRECT_HOSTS` is refused regardless of which path registered
it. Deregistering a client invalidates every authorization code and refresh
token it holds, in the same transaction as the client row going away — and
takes effect on any access token already in flight too, since
`AuthService._authenticate_oauth` looks the issuing client up on every
request. Deregistering a compromised client cuts it off now, not in half an
hour when its current access token would have expired.

### Second factors

Opt-in, per account. A user with no second factor logs in exactly as before;
enrolling one changes nothing about anyone else's login. Two methods:

- **An authenticator app** (TOTP, RFC 6238) — a phone or a laptop can each
  hold one, since `ALLOWS_MULTIPLE` is true for this kind.
- **Backup codes** — ten single-use recovery codes, for when the
  authenticator is gone but the account is not. Generating a set replaces
  any previous one; there is only ever one live set.

`POST /token` answers a password alone with a token, same as always. For an
enrolled account it answers with a challenge instead, redeemed at
`POST /token/mfa`:

```bash
curl -s -X POST localhost:8000/token -d 'username=YOU&password=YOURPASS'
# {"mfa_required": true, "mfa_token": "...", "methods": ["totp"], "expires_in": 300}

curl -s -X POST localhost:8000/token/mfa -d 'mfa_token=...&code=123456'
# {"access_token": "...", "token_type": "bearer"}
```

Both steps are `application/x-www-form-urlencoded`, matching `/token` itself.
The challenge is a signed JWT, not a database row — nothing is written to
issue it, and it expires on its own in five minutes by default
(`MFA_CHALLENGE_TTL_MINUTES`). It is bound to the password hash in force when
minted, so a password change invalidates every challenge outstanding against
the old one, and it is scoped to the surface that issued it: a challenge from
`/token` cannot be redeemed at the docs login or the OAuth authorize flow, and
the reverse. The docs login and `POST /oauth/authorize` both gain the same
second step, in the same shape, for the same reason.

A wrong code costs exactly what a wrong password costs against the throttle
below — six digits is a small enough space that leaving it unthrottled would
make guessing practical.

**TOTP secrets are encrypted at rest**, with Fernet under `MFA_ENCRYPTION_KEYS`,
so a database read alone does not mint a second factor for someone's
account — the key lives in the application process, not the database, so
this is not a defense against a compromised host, only against what a leaked
backup, a SQL-injection read, or a database dump pasted into a support ticket
can do on its own. Backup codes are hashed rather than encrypted, the same
way API keys are, which is what lets them keep working if the encryption key
is ever lost — there is nothing there to decrypt.

**Recovery.** An admin can strip every second factor from an account with
`DELETE /users/{id}/mfa`, which — unlike the lock-clearing route — requires
an interactive login, not just `users:admin`: a key that could disable MFA
would make MFA optional service-wide for whoever steals it. When there is no
admin to ask, `python -m admin_cli reset-mfa <username>` does the same thing
directly against the database, and needs no encryption key to do it — it
deletes rows rather than reading them, which is what makes it the break-glass
path when `MFA_ENCRYPTION_KEYS` itself is the thing that is wrong or lost.
Backup codes exist precisely so neither of these is usually needed.

**Back the encryption key up somewhere other than beside the database dump.**
A backup containing both the encrypted secrets and the key that opens them is
a backup with no encryption.

## Pointing an MCP client at a key

The key belongs to **whoever owns the documents** — a document is reachable
only by its owner, so a key minted for someone else reads an empty store.

Log in as that person, then mint a key scoped to the three reads and nothing
else. No write, no delete: a tailoring client has no reason to change anything,
and this is the credential most likely to end up in a config file on a laptop.

```bash
TOKEN=$(curl -s -X POST localhost:8000/token -d 'username=YOU&password=YOURPASS' | jq -r .access_token)
```

```bash
curl -s -X POST localhost:8000/api-keys -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"name":"mcp-server","scopes":["resume:read","metadata:read","skill:read"]}' | jq -r .key
```

Narrower still is possible. `retrieve_resume_data` needs `resume:read`, since
the résumé is the one argument it requires, but the two companions are checked
only when their ids are passed — so a key with `resume:read` and `skill:read`
can follow the instructions against the résumé without ever reading the
metadata. `list_resume_documents` reports only the types a key may read, so a
narrowed one sees a smaller store rather than a refusal.

Asking for a companion the key may not read is refused rather than answered
with `null`. `null` means "not stored", and a client told that would go on to
work without a document that exists.

The secret is shown once. Seed the instructions the client will follow, from
one of the `examples/` fixtures — the résumé and the metadata go in the same
way, through their own routes:

```bash
curl -s -X POST localhost:8000/documents/skill -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d @examples/resume.skill.example.json | jq
```

```json
{
  "name": "resume.skill.example.json",
  "revision_id": 1,
  "status": "created"
}
```

`status` is `"created"` when the write produced a new revision, or `"unchanged"`
when `data` and `public` both matched the current revision exactly — in that
case `revision_id` is the existing revision's, not a new one, and
`revision_note` is silently discarded: there is no new revision to attach it
to. Re-running the command above a second time with the same file returns
`"unchanged"`.

Manage keys with `GET /api-keys` (metadata only — the secret is never
retrievable) and `DELETE /api-keys/{id}`. Revocation is a soft delete, so
`last_used_at` stays readable afterward; that per-client audit trail is most
of the reason to prefer keys over a shared password.

See [`docs/mcp-clients.md`](docs/mcp-clients.md) for wiring the key into a
specific MCP client, and [`docs/oauth-setup.md`](docs/oauth-setup.md) for the
OAuth alternative.

### Failed logins are throttled

Password logins are counted, per account and per calling address. Five failures
within fifteen minutes lock the account for fifteen minutes; each further lock
with no successful login in between doubles the wait — 15, then 30, then 60 —
and the fourth is permanent until an admin clears it. A locked account gets
`429` with `Retry-After`; a permanently locked one gets a plain `401`, because
there is no wait to communicate and nothing else to disclose.

`/token`, the docs login, and `POST /oauth/authorize` all go through the same
`authenticate_user`, so none of the three can be used to walk around the
counter on the others. A wrong MFA code counts the same way, against the same
account and address tallies, whichever of the three surfaces the second step
is on — see "Second factors" above.

**A successful login resets the ladder.** That is the property that makes a
permanent lock safe to have: an account someone actually uses cannot be walked
up to one by an outsider guessing at it, because every real login puts the
escalation back on the bottom rung.

**A lock closes the password path only.** It is deliberately not the `active`
flag — that one is checked on every credential, so clearing it would kill live
sessions and API keys too. An admin locked out of `/token` can still act with
a key they already hold, including calling `DELETE /users/{id}/lock` on
themselves. When there is no such key, `python -m admin_cli unlock <username>`
does the same thing against the database directly; `--list` shows what is
locked and `--address` clears a banned address.

The per-address tally catches what the per-account one cannot see: one guess
each across a list of usernames, where no single account accumulates enough
failures to lock. Twenty failures in fifteen minutes bans the address for
fifteen. Never permanently — an address is a lease, not an identity.

The address is taken a fixed number of hops from the **right** of
`X-Forwarded-For`, not the leftmost entry — see `AUTH_TRUSTED_PROXY_HOPS` in
[`docs/configuration.md`](docs/configuration.md) for how to set that correctly
behind whatever sits in front of this service, and why getting it wrong throttles
every caller as the proxy.

API keys are not throttled. They are 32 bytes of randomness looked up by an
indexed prefix — there is no dictionary to attack and no slow hash to exhaust,
so a lockout would add a denial-of-service surface and no defense.

Every threshold is configurable; see [`docs/configuration.md`](docs/configuration.md).

### The docs are not public in production

FastAPI serves `/docs`, `/redoc` and `/openapi.json` without a credential — a
complete index of every route, payload shape and security scheme. Where
`ENVIRONMENT=production`, all three sit behind a login instead — an HTML form,
not HTTP Basic: a username and password, then a code for an MFA-enrolled
account, then a signed `HttpOnly` cookie good for an hour by default
(`DOCS_SESSION_MINUTES`).

All three routes, not just the two pages: they are only renderers for the
schema, so guarding them and leaving `/openapi.json` open would guard nothing.

The credential is checked by the same `authenticate_user` call `/token` makes.
There is no separate docs password to rotate — the same accounts work, a
passwordless service account still cannot log in, and deactivating a user or
changing their password closes this door with the rest, the cookie included:
it is bound to the password hash in force when it was issued. Any active
account will do; this gates who read the route list, not what they can call,
and every route behind it still enforces its own scopes.

An HTML form rather than a bearer token because this is the one surface opened
by a browser, which has nowhere to keep a token but will hold a cookie for you.
It is also, deliberately, the only surface in this service that takes a
cookie at all — `CLIENT_ALLOWED_ORIGINS`'s null-origin caveat and
`middleware/client_cors.py`'s "no `Access-Control-Allow-Credentials`" both
still hold, because nothing outside `routers/docs.py` reads this cookie and
the header that would let cross-origin JavaScript see a credentialed response
is never sent. Development leaves the docs open: a prompt in front of
localhost only trains people to type one.

## Running it

Three ways to run this service, each documented end to end:

- [`docs/running.md`](docs/running.md) — native Python, Docker, or Compose.
- [`docs/local-only.md`](docs/local-only.md) — SQLite, a bootstrap admin, and
  the browser client, all on `localhost`, with no domain, no TLS, no cloud
  account and no OAuth.
- [`docs/configuration.md`](docs/configuration.md) — every setting, grouped
  by concern.

Hosting a public instance behind a domain — TLS, a reverse proxy, a managed
Postgres, CI/CD — is out of scope for this repository; the two docs above
cover everything this repository is responsible for.

## Tests

```bash
uv run pytest
```

```bash
uv run pytest --cov --cov-report=term-missing
```

The suite runs against a temporary database with a generated signing key, and
pins every setting it depends on before importing the application. It does not
read your `.env`, touch `data/app.db`, or assume any particular local
configuration — identifiers are read back from the rows the fixtures create
rather than written down. Migrations are the real ones, run once per session.

CI runs the same suite with `--cov-fail-under=90` on every push — see
`.github/workflows/test.yml`.

## Before committing

Formatting, linting and type checking run as a git hook rather than in CI.
Install it once per clone:

```bash
uv sync --all-groups && uv run pre-commit install
```

That gives you `ruff check --fix` (which includes import sorting), `ruff
format`, and `ty check` on every commit. The first two rewrite files in place,
so a commit they touch fails once and needs the files re-staged.

`alembic/versions/` is excluded from both tools — the scaffolding there is
generated, and the schema is covered by the tests instead. Note that pre-commit
hands ruff explicit filenames, which overrides the exclusion, so both ruff hooks
pass `--force-exclude`. Anything invoking ruff outside pre-commit needs the same
flag to see the same result.

`ty` runs over the whole project rather than the staged files, because a
signature change surfaces in a different file than the one you edited.
pre-commit stashes unstaged work first, so what gets checked is what would
actually land.

To run them by hand:

```bash
uv run pre-commit run --all-files
```

## The website

The public projection needs no credential:

```
GET /public/{username}/resume/resume.json
```

Redaction is structural rather than conditional. `ResumePrivate` and
`ResumePublic` are two separate model trees, not a subclass relationship —
the public and private payloads are separate routes with separate response
models, and `PublicProjection.build` re-validates the redacted data through
`ResumePublic` under `extra="forbid"`: a private field that survived the
redaction pass has no field to land in and raises instead of serving.
`tests/test_public_projection.py` holds this two ways — a structural-drift
test that walks both trees and asserts every field difference between them is
a declared one, and a sentinel-leak test that populates every private field
with a unique marker and asserts none of them reach the projection.

The resume payload is a JSON Resume 1.0 document
(https://jsonresume.org/schema), with two deliberate deviations: `work[].highlights[]`
and `skills[].keywords[]` carry objects rather than the plain strings the
schema specifies, since tailoring needs the specifics, tech, and per-keyword
rating those objects carry. The feed keeps that shape rather than flattening
to strict schema conformance, so the website can render highlight specifics
and per-skill urls.

Those routes answer with `Access-Control-Allow-Origin: *`, so any site can
`fetch()` them directly — no proxy and no per-site configuration. A minimal
consumer:

```js
const res = await fetch("https://your-host/public/alice/resume/resume.json");
const resume = await res.json();
```

Nothing else in the service carries the header by default;
`middleware/public_cors.py` explains why it is a wildcard rather than an
allowlist, and why it is stamped whether the caller sent an `Origin`.

### Withholding a single entry

`Document.public` publishes or unpublishes an entire document. A work entry,
highlight, skill, skill keyword, certificate, education entry, project,
profile or location also carries its own `publish: bool` (default `true`),
which withholds just that one entry from the public feed while leaving it in
the private payload the MCP client reads.

This is curation, not a security control — the fields that are actually
sensitive (`basics.email`, `basics.phone`, `highlights[].tech`,
`highlights[].metrics`, `highlights[].story`, `keywords[].level`,
`keywords[].lastUsed`, `fineTuningData`, …) are redacted structurally by the
public/private model split above, regardless of `publish`. `publish: false`
only decides which *entries* show up, never which *attributes* of a shown
entry are visible.

Withholding a work entry or skill drops it entirely; withholding every
highlight under a still-published work entry keeps the entry with an empty
highlights list, since the employment record itself is load-bearing.
Withholding every keyword under a still-published skill drops the skill,
since a heading with nothing under it is a rendering artifact. See
`PublicProjection` in `services/document/dtos/resume_object.py` for the exact
rules.

The flag is also, deliberately, silent to a tailoring run: it says nothing
about whether an entry belongs in a document written for one specific
application. An entry kept off the public site for stealth or curation
reasons is often exactly the one worth leading with in an application — see
the `publish` field's description in `GET /documents/schemas`, and
`disclosure.rules` in the metadata document.

**Withholding is not immediate.** `/public/*` sits behind a Cloudflare cache
rule, and the site additionally caches the feed client-side in `localStorage`
for 30 minutes (`CACHE_TTL_MS` in the frontend's `remote.ts`). An entry
withheld for a reason that feels urgent needs a cache purge, not just a
document write.

## Browser client

[`resume-cv-mcp-api-web-client`](https://github.com/tristankerner/resume-cv-mcp-api-web-client)
is a single-page UI for managing documents, API keys, second factors and — for
an admin — users and OAuth clients. See its README for what's there and how to
run it. It is a separate repository, not a submodule: this API has no
build-time dependency on any client, and nothing here needs one to be present.
[Docker Compose can also build and bake it in for you](#with-the-web-client) —
this section is for developing against a manual checkout instead.

To develop against it, clone it wherever you like, build it once
(`npm ci && npm run build`), and point `CLIENT_HTML_PATH` at the
**build output**, `dist/index.html`:

```bash
git clone https://github.com/tristankerner/resume-cv-mcp-api-web-client clients
```

```bash
CLIENT_HTML_PATH=./clients/dist/index.html
```

`clients/index.html` is not that file — it is the Vite entry stub, and serving
it here yields a blank page whose `/src/main.tsx` request 404s against this
API. `./clients` is the conventional name — `.dockerignore` expects it when
keeping a local build out of an image — but nothing enforces it. If the path
does not exist, `GET /client` declines to register and logs a warning naming
the file it looked for; nothing else is affected.

Unlike the public projection above, the routes the client calls (`/token`,
`/documents`, `/api-keys`, `/users/*`) are authenticated, so they cannot use a
wildcard: a bearer token is only as private as the set of origins allowed to
read the response that might carry it. `CLIENT_ALLOWED_ORIGINS` is an explicit,
comma-separated allowlist instead, enforced by `middleware/client_cors.py`,
which echoes back an allowlisted `Origin` rather than stamping `*`. Empty by
default, matching every other route not covered by `PublicCorsMiddleware` —
set it before pointing any browser-based client at this service, or every
request will reach the server and have its response discarded unread. See
[`docs/configuration.md`](docs/configuration.md) for the `null`-origin caveat.

`CLIENT_HTML_PATH` sidesteps all of that by serving the client same-origin:
set it and `GET /client` returns that file. Which file is the deployment's
business — it copies a client into the build context before `COPY . /app`, and
this repository never learns which one. The client's build produces a single
self-contained HTML file, which is what belongs here. `.dockerignore` still
keeps a local `clients/dist/` and `clients/node_modules/` out of any
image: those are whatever the last local build left behind, gitignored and so
unreviewed, and a deployment composes its own copy of the built file in
instead. Unset by default, so the API itself depends on no build artifact.

This is the path of least resistance for putting the client in front of a
deployed API. `CLIENT_ALLOWED_ORIGINS` does not need to name it, no static
host is involved, and the client pre-fills its API base URL from its own
origin — a page served at `/client` knows where the API is, so nobody has to
type it. The response also carries `frame-ancestors 'none'`, `nosniff`, and
`Referrer-Policy: no-referrer`; `main.py`'s `ClientRoute.HEADERS` says what is
deliberately absent from that list and why.

## Databases

`.env.example` points `DATABASE_URL` at SQLite, and that is the whole local
story — `data/app.db`, no service to run. Point it at Postgres and nothing else
changes:

```
DATABASE_URL=postgresql+asyncpg://user:pass@host/resume_api?ssl=require
```

`alembic/env.py` rewrites that to `postgresql+psycopg` for the migrations,
translating `ssl` to lib's `sslmode` on the way, so one setting drives both
engines. `documents` carries a no-update trigger per dialect — a statement
trigger on SQLite, a trigger function on Postgres — so a revision cannot be
rewritten. Deletion is not blocked there; it is gated on the type's delete
scope instead.

The test suite runs on SQLite regardless of what `DATABASE_URL` says.

## Layout

```
routers/          HTTP routes, and the MCP tool
services/auth/    authentication, scopes, Principal, API keys, login throttling, MFA (services/auth/mfa/)
services/user/    user CRUD, first-admin bootstrap, and seeding a new account's example documents (document_seeder.py)
services/oauth/   OAuth 2.1 authorization server, and admin client registration (client_admin_service.py)
services/document/ document reads, writes, and the public projection
persistence/      SQLAlchemy models
alembic/          migrations, run automatically at startup
admin_cli.py      last-resort account recovery direct against the database — see "Administration" above
```

`admin_cli.py` is the one root script now — `unlock`, `reset-password`,
`reset-mfa` and `bootstrap-admin` used to be four separate ones. Its
`reset-mfa` subcommand is the break-glass path for an account whose second
factor and every backup code are gone; see "Second factors" above.
