# Connecting MCP clients with an API key

How to point an MCP client at this service's tool server using a static API
key. No OAuth involved.

The server also speaks OAuth 2.1 — see [oauth-setup.md](oauth-setup.md) — and
for a client that supports it, that is the better route: no key to place in a
config file, and the grant can be narrowed and revoked on its own. This page
is for the clients that cannot, and for scripts and local development, where a
header is simpler than a browser flow. Both paths work; neither replaces the
other.

## What you need

| | |
| --- | --- |
| Endpoint | `https://mcp.example.com/resume/mcp` |
| Transport | Streamable HTTP (not SSE, not stdio) |
| Auth | `Authorization: Bearer <api key>` |
| Scopes | `resume:read`, `metadata:read`, `skill:read` |

Mint the key as the user who **owns** the documents — reads are scoped to the
owner, so a key belonging to anyone else authenticates fine and sees an empty
store:

```bash
curl -sS -X POST https://mcp.example.com/api-keys -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"name":"mcp-client","scopes":["resume:read","metadata:read","skill:read"]}' | jq -r .key
```

The secret is shown once. Give it the three reads and nothing else, so a leak
cannot write documents, delete them, or touch users.

**Check it before configuring anything.** This isolates a server or key problem
from a client problem, and takes a second:

```bash
curl -sS -X POST https://mcp.example.com/resume/mcp -H "Authorization: Bearer $MCP_KEY" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
```

A JSON-RPC result means the server and key are good. If this fails, no client
config will help.

## On storing the key

Every method below puts a credential somewhere on disk. Prefer the ones that
read an environment variable or prompt, and where a file is unavoidable, check
its permissions. Do not commit any of these files.

---

## Claude Code

Supports remote HTTP servers with headers directly — no bridge.

```bash
claude mcp add --transport http --scope user --header "Authorization: Bearer YOUR_KEY" resume https://mcp.example.com/resume/mcp
```

Options first, then the name, then the URL — the order `claude mcp add --help`
documents.

`--scope` defaults to `local`, which is this directory only. `user` makes it
available in every project. `project` writes a `.mcp.json` into the working
directory — do not use it here, as that file would commit the key.

Verify:

```bash
claude mcp list
```

## Claude Desktop

The desktop app takes **stdio** servers in its config file, so a remote HTTP
server needs the `mcp-remote` bridge to translate. Node is required.

Config file location:

| OS | Path |
| --- | --- |
| Linux | `~/.config/Claude/claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "resume": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://mcp.example.com/resume/mcp",
        "--header",
        "Authorization: Bearer YOUR_KEY"
      ]
    }
  }
}
```

**Close Claude Desktop completely before editing this file.** The app holds
its own copy of the file's contents in memory and rewrites the whole file from
that copy — so an edit made while it is running is discarded, usually within a
minute or two, with no error and no trace. Quit the app fully (check the tray
or system indicator; closing the window may not be enough), edit, save, then
start it again.

Two further cautions, both learned the hard way:

- **The file must be valid JSON.** A stray trailing comma before the final `}`
  invalidates the whole document. The app cannot read it, and may regenerate it
  from defaults — taking your `mcpServers` block with it. Validate before
  restarting: `jq empty ~/.config/Claude/claude_desktop_config.json`
- **On the Linux build, this file also holds the app's own preferences**
  (`coworkUserFilesPath`, `preferences`, sidebar and Cowork settings). Merge
  your `mcpServers` key into the existing top-level object rather than
  replacing the file, and keep a copy of the original first.

If the block keeps disappearing despite the app being closed, that build is
managing the file itself and hand-editing will not stick. Use Claude Code
above, which writes to a config the desktop app does not own.

## Gemini CLI

Supports remote HTTP with headers natively — no bridge.

```bash
gemini mcp add --transport http --header "Authorization: Bearer YOUR_KEY" resume https://mcp.example.com/resume/mcp
```

Or by hand in `~/.gemini/settings.json` (user-wide) or `.gemini/settings.json`
(project). Note the key is `httpUrl`, not `url`:

```json
{
  "mcpServers": {
    "resume": {
      "httpUrl": "https://mcp.example.com/resume/mcp",
      "headers": {
        "Authorization": "Bearer ${RESUME_MCP_KEY}"
      }
    }
  }
}
```

Gemini CLI expands `$VAR` and `${VAR}` in both `httpUrl` and `headers`, so the
key can live in your environment rather than in the file. Prefer that.

## GitHub Copilot

Both surfaces support headers directly.

**VS Code** — `.vscode/mcp.json` in a workspace, or the user-level MCP config.
`"type": "http"` is **required**; without it VS Code assumes stdio and tries to
execute the URL as a command.

```json
{
  "servers": {
    "resume": {
      "type": "http",
      "url": "https://mcp.example.com/resume/mcp",
      "headers": { "Authorization": "Bearer ${input:resumeMcpKey}" }
    }
  },
  "inputs": [
    {
      "id": "resumeMcpKey",
      "type": "promptString",
      "description": "resume-cv-mcp-api MCP key",
      "password": true
    }
  ]
}
```

The `inputs` block makes VS Code prompt for the key and store it in its secret
storage instead of the file — which matters here, because `.vscode/mcp.json` is
usually committed.

**Copilot CLI**:

```bash
copilot mcp add --transport http --header "Authorization: Bearer YOUR_KEY" resume https://mcp.example.com/resume/mcp
```

Config lives at `~/.copilot/mcp-config.json` if you prefer to edit it directly.

**Known issue:** Copilot CLI has an open bug where an HTTP server returning a
`401` with `WWW-Authenticate: Bearer` sends it into OAuth discovery instead of
falling back to the configured header — see
[copilot-cli#3100](https://github.com/github/copilot-cli/issues/3100). This
server does return exactly that, so if Copilot CLI reports an auth failure
while the `curl` check above succeeds, this is why, and it is not something the
config can work around. Adding headers through the Copilot GUI has a separate
open bug, [copilot-cli#2849](https://github.com/github/copilot-cli/issues/2849);
prefer the CLI or the config file.

## ChatGPT

**Not possible with an API key.** ChatGPT's Developer Mode custom connectors
support Streamable HTTP and SSE, but authentication is limited to **OAuth or
none** — there is no field for a static header, and passing a key as a URL
query parameter is both unsupported here and liable to be rejected as unsafe.

For reference, Developer Mode lives under **Settings → Apps → Advanced
settings** (Workspace Settings → Permissions & Roles on Enterprise), and is
available on Pro, Plus, Business, Enterprise and Education plans on the web app.

**Use OAuth instead** — see [oauth-setup.md](oauth-setup.md). The server
supports it, and it is the only way ChatGPT connects to this endpoint at all.
Registration is closed by default, so pre-register a client and paste the
`client_id` and `client_secret` into the connector rather than expecting it to
register itself.

## Client support at a glance

| Client | Static bearer header | Bridge needed | Notes |
| --- | --- | --- | --- |
| Claude Code | yes | no | `claude mcp add` |
| Claude Desktop | via bridge | **yes** | close the app before editing config |
| Gemini CLI | yes | no | `httpUrl` + `headers`, expands env vars |
| Copilot (VS Code) | yes | no | `"type": "http"` required |
| Copilot CLI | yes | no | see the OAuth-discovery bug above |
| ChatGPT | **no** | — | OAuth only — see [oauth-setup.md](oauth-setup.md) |

## If a client will not connect

Work down this list; each step rules out a layer.

0. **Rule out the CDN first.** If the service sits behind Cloudflare, its
   Browser Integrity Check answers non-browser clients with `403` and
   `error code: 1010` **at the edge**, so the request never reaches the
   application and leaves no trace in its logs. MCP traffic is
   server-to-server and has no browser signature, so this hits it squarely.
   Compare two user agents — a `403` for one and `200` for the other means the
   edge is filtering, not the service:

   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' -A 'Python-urllib/3.14' https://mcp.example.com/.well-known/oauth-authorization-server
   ```

1. **Run the `curl` check above.** Failure means the server or the key, not the
   client.
2. **Check the key's scopes.** A key missing one of the three reads connects
   successfully and silently exposes fewer tools. `GET /api-keys` lists them.
3. **Check the key's owner.** A valid key belonging to the wrong user connects
   and returns an empty store.
4. **Confirm the transport is Streamable HTTP**, not SSE. This server is
   `http_app()`; SSE is a different endpoint shape and will not negotiate.
5. **For file-based configs, validate the JSON** — `jq empty <file>`. A syntax
   error usually produces no error message in the client, just an absent server.
6. **For Claude Desktop specifically**, confirm the app was fully closed when
   you edited, and re-check the file after restarting to see whether your block
   survived.
