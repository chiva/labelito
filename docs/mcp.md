# MCP server

labelito can expose a [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server so an
AI client — Claude Desktop, an agent, or any MCP-capable app — can generate and print labels through
tools instead of raw HTTP calls. It reuses the exact same rendering, validation, printing,
idempotency, media pre-flight, and history logic as the REST API, so anything the tools do behaves
identically to the corresponding endpoint.

- [Enabling it](#enabling-it)
- [Transport & endpoint](#transport--endpoint)
- [Authentication](#authentication)
- [OAuth 2.0 / OIDC authentication (external IdP)](#oauth-20--oidc-authentication-external-idp)
- [Behind a reverse proxy](#behind-a-reverse-proxy)
- [Tools](#tools)
- [Connecting a client](#connecting-a-client)

## Enabling it

Two environment variables gate the server (both default off — see
[configuration reference](configuration.md#environment-variables)):

| Variable | Default | Effect |
|---|---|---|
| `MCP_ENABLED` | `false` | Mount the MCP server at `/mcp`. While `false` the endpoint is absent (404). |
| `MCP_WRITABLE` | `false` | Also register the **write** tools (print / reprint). While `false`, only read-only tools exist and a connected client never even sees the write tools. |

```yaml
# docker-compose.yml (excerpt)
environment:
  API_TOKEN: "a-long-random-secret"   # /mcp reuses this
  MCP_ENABLED: "true"
  MCP_WRITABLE: "true"                 # omit / false to keep it read-only
```

On boot the log confirms the mode:

```text
MCP server enabled at /mcp (read+write)
```

## Transport & endpoint

The server speaks **streamable HTTP** and is mounted at **`/mcp`** on the *same* port and app as the
web UI and REST API (there is no separate MCP port). Responses are plain JSON and each call is
stateless, so a single `POST` carries a full JSON-RPC request. The canonical URL for a local
deployment is **`http://localhost:8765/mcp/`** — note the **trailing slash**.

The route lives at `/mcp/`; a bare `POST /mcp` (no slash) **307-redirects** to `/mcp/`, which most
clients follow automatically. Prefer the trailing-slash form: it skips the redirect hop and is
robust behind a **TLS-terminating reverse proxy**, where following the 307 can drop the
`Authorization` header or downgrade the redirect to `http://` (see
[Behind a reverse proxy](#behind-a-reverse-proxy)).

## Authentication

The `/mcp` endpoint is guarded by the **same credentials as the rest of the protected API**: a valid
`Authorization: Bearer <API_TOKEN>` **or** HTTP Basic (`WEB_AUTH_USER` / `WEB_AUTH_PASSWORD`). It
therefore inherits the fail-closed startup guard — the service refuses to start unless one auth mode
(or an explicit `ALLOW_UNAUTHENTICATED=true`) is configured. MCP clients typically send the bearer
token. In unauthenticated mode the endpoint is open, exactly like the rest of the API.

> DNS-rebinding Host/Origin validation is disabled on the `/mcp` mount (a self-hosted service is
> reached at an arbitrary, deployment-specific host/IP), so the bearer/Basic auth above plus network
> placement are the access controls — keep `API_TOKEN` set on any network-reachable deployment.

## OAuth 2.0 / OIDC authentication (external IdP)

Many AI MCP clients (ChatGPT connectors, Claude connectors, …) authenticate via the MCP
Authorization spec rather than a hand-configured static header: they discover an OAuth Authorization
Server, register themselves with [Dynamic Client Registration (DCR, RFC 7591)][dcr], log the user in
via OpenID Connect, and present the resulting bearer **JWT** access token. labelito supports this as
an opt-in, **additive** layer on `/mcp`.

labelito acts only as an OAuth 2.0 **Resource Server**. Your **existing OIDC provider** (Keycloak,
Authentik, Zitadel, …) is the Authorization Server and handles DCR + login; it must support DCR for
DCR-only clients to self-register. labelito never becomes an authorization server — it publishes
[RFC 9728][rfc9728] Protected Resource Metadata pointing at your issuer and validates the token
(signature via the issuer's JWKS, plus `iss` / `aud` / `exp` / scopes).

**This is additive:** with OIDC on, `/mcp` *still* accepts the static `API_TOKEN` bearer and HTTP
Basic. It also covers **only `/mcp`** — the REST API and web UI stay on bearer/Basic. If you want
those protected too, keep `API_TOKEN` (or `WEB_AUTH_*`) set. Because OIDC does not protect the REST
API, it does **not** satisfy the fail-closed startup guard on its own: an OIDC-only deployment must
still set `API_TOKEN`/`WEB_AUTH_*`, or explicitly acknowledge the open REST surface with
`ALLOW_UNAUTHENTICATED=true`.

The handshake (all automatic in a compliant client): the client `POST`s to `/mcp/` → gets `401` with
`WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp"` → fetches
that metadata → discovers your Authorization Server → does DCR + OIDC login → obtains a token →
retries with `Authorization: Bearer <token>`.

| Variable | Default | Effect |
|---|---|---|
| `OIDC_ENABLED` | `false` | Master opt-in. When `true`, `/mcp` also accepts validated OIDC JWTs and the metadata endpoint is published. |
| `OIDC_ISSUER` | – | Issuer URL exactly as it appears in the token `iss` (e.g. `https://idp.example.com/realms/labelito`). Advertised as the Authorization Server. **Required** when enabled. |
| `OIDC_AUDIENCE` | – | Expected `aud` — the resource identifier you configure the IdP to mint `/mcp` tokens for (normally labelito's public `/mcp` URL). **Required** when enabled. |
| `OIDC_REQUIRED_SCOPES` | – | Space-separated scopes a token must carry (e.g. `labelito.print`). Empty = no scope requirement. |
| `OIDC_JWKS_URI` | – | Explicit JWKS endpoint; bypasses discovery. |
| `OIDC_DISCOVERY` | `true` | When JWKS is unset, resolve it from `{issuer}/.well-known/openid-configuration`. |
| `OIDC_ALGORITHMS` | `RS256` | Space-separated allowlist of accepted JWT signing algorithms (set `ES256` for EC keys). Blocks `alg:none`/HMAC confusion. |
| `OIDC_LEEWAY_SECONDS` | `60` | Clock-skew tolerance for `exp`/`nbf`/`iat`. |

At your IdP: register a resource / API whose identifier equals `OIDC_AUDIENCE` (typically the public
`/mcp` URL), optionally define a scope (e.g. `labelito.print`), and enable Dynamic Client
Registration so MCP clients can self-register. Then:

```yaml
# docker-compose.yml (excerpt)
environment:
  MCP_ENABLED: "true"
  MCP_WRITABLE: "true"
  # OIDC Resource Server — validate tokens from your external IdP (additive to API_TOKEN):
  OIDC_ENABLED: "true"
  OIDC_ISSUER: "https://idp.example.com/realms/labelito"
  OIDC_AUDIENCE: "https://labelito.example.com/mcp"
  OIDC_REQUIRED_SCOPES: "labelito.print"
  # Keep API_TOKEN set to also protect the REST API / web UI (OIDC covers only /mcp):
  API_TOKEN: "a-long-random-secret"
```

Invalid/expired/wrong-audience tokens get `401`; an authentic token missing a required scope gets
`403 insufficient_scope`; a JWKS/discovery outage fails **closed** (`401`, never treated as valid).
Behind a reverse proxy, set `FORWARDED_ALLOW_IPS` and (for sub-paths) `PROXY_PATH_HEADER` so the
advertised `resource` URL matches what the client reached — see
[Behind a reverse proxy](#behind-a-reverse-proxy).

[dcr]: https://www.rfc-editor.org/rfc/rfc7591
[rfc9728]: https://www.rfc-editor.org/rfc/rfc9728

## Behind a reverse proxy

Two things make a proxied deployment (Traefik, nginx, Caddy, …) work cleanly:

1. **Use the trailing-slash URL** — point the client at `https://your-host/mcp/`, not `/mcp`. This
   avoids the `/mcp` → `/mcp/` 307 entirely, so there is no redirect hop for the proxy or client to
   mishandle. If the proxy serves labelito under a **path prefix** (`PROXY_PATH_HEADER`), include it:
   `https://your-host/labelito/mcp/`.
2. **Let the app trust the proxy's forwarded headers** so it knows the request arrived over `https`.
   The server (uvicorn) ignores `X-Forwarded-Proto`/`-For`/`-Host` unless the request's source IP is
   trusted, so with a TLS-terminating proxy any redirect it *does* emit (e.g. the bare-`/mcp` 307) is
   built as `http://…` and breaks. Set **`FORWARDED_ALLOW_IPS`** to the source address the proxy
   connects from (the peer IP labelito sees) — its container subnet (e.g. `172.18.0.0/16`) or the
   Docker bridge gateway, not necessarily `127.0.0.1`. The [reverse-proxy guide](reverse-proxy.md#trusting-the-proxy-forwarded_allow_ips)
   explains how to find the right value (and why the container case surprises people).

With both in place, a client pointed at `https://your-host/mcp/` with `Authorization: Bearer
$API_TOKEN` connects with no redirect and no scheme downgrade.

For proxy setup itself — Traefik/nginx/Caddy examples, sub-path hosting, and the forwarded-header
settings — see [reverse-proxy deployment](reverse-proxy.md).

## Tools

Read-only tools are always registered when `MCP_ENABLED=true`; write tools require
`MCP_WRITABLE=true`.

### Read-only

| Tool | What |
|---|---|
| `list_templates` | List templates with their field contracts and required media. |
| `get_template(name)` | One template's fields (always), plus its raw `yaml` source — included only when both `EDITOR_ENABLED` and `TEMPLATES_LOADABLE` are true, else `null`. |
| `get_capabilities` | Printer model, dpi, supported labels, and geometries. |
| `get_printer_status` | Live printer state: loaded media, model, fault/error bits. |
| `preview_label(template, fields, …)` | Render a PNG preview of a **stored** template — nothing printed or saved. |
| `preview_ephemeral_label(yaml, fields, …)` | Render a PNG preview of an **inline** template designed on the fly. |
| `list_history(limit, offset)` | Browse recorded print jobs, newest first. |
| `get_history_label(job_id)` | One recorded job's full detail. |

### Write (require `MCP_WRITABLE=true`)

| Tool | What |
|---|---|
| `print_label(template, fields, …)` | Print a stored template. Supports `copies`, `dry_run`, render options, `idempotency_key`, and `{{seq}}` sequences. |
| `print_ephemeral_label(yaml, fields, …)` | Print an **ephemeral** label from an inline YAML body — never written to disk, but recorded in history (with the frozen body) so it can be reprinted. |
| `reprint_history_label(job_id)` | Reprint a past job exactly (same template, fields, options, and computed dates). |

Errors (unknown template, missing required fields, invalid YAML, media mismatch, printer
unreachable, …) surface to the client as a tool error carrying the underlying reason.

History tools follow the same two gates as the REST browse routes:

- `HISTORY_MODE` (storage): with `disabled`, there is nothing to browse or reprint.
- `HISTORY_UI` (browse visibility): with `false`, `list_history` and `get_history_label` are hidden
  and error (mirroring the REST `/history` routes' 404), while `reprint_history_label` stays
  available — exactly like reprint-by-id on the REST surface.

## Connecting a client

Any MCP client that speaks **streamable HTTP** can connect: point it at the trailing-slash URL
(`http://localhost:8765/mcp/`, or `https://your-host/mcp/` behind a proxy — add the external path
prefix under a `PROXY_PATH_HEADER` sub-path deployment, e.g. `https://your-host/labelito/mcp/`) and
add an `Authorization: Bearer <API_TOKEN>` header. Below are the three most common clients; each links
to its own MCP docs, which are the source of truth if the syntax has moved on. Prefer keeping the
token in an environment variable over hardcoding it.

### Claude Code

Add it with one command ([MCP docs](https://code.claude.com/docs/en/mcp)):

```bash
claude mcp add --transport http --header "Authorization: Bearer $API_TOKEN" \
  labelito https://your-host/mcp/
```

`--scope user` makes it available in every project; `--scope project` writes a shared `.mcp.json`
you can commit. The resulting entry is:

```json
{
  "mcpServers": {
    "labelito": {
      "type": "http",
      "url": "https://your-host/mcp/",
      "headers": { "Authorization": "Bearer a-long-random-secret" }
    }
  }
}
```

### Claude Desktop

Claude Desktop's `claude_desktop_config.json` loads **stdio** servers, not a remote `url`, so pick
one of ([connector docs](https://modelcontextprotocol.io/quickstart/user)):

- **Connectors UI** — Settings → Connectors → *Add custom connector*, paste `https://your-host/mcp/`.
  This path suits OAuth or unauthenticated servers; it has no field for a static bearer header, so for
  labelito's token auth use the bridge below.
- **`mcp-remote` bridge** — wrap the remote endpoint as a stdio server with
  [`mcp-remote`](https://github.com/geelen/mcp-remote) and restart the app. Pass the header value
  through an env var (`AUTH_HEADER`) and reference it **without a space** so `mcp-remote` parses it
  correctly:

  ```json
  {
    "mcpServers": {
      "labelito": {
        "command": "npx",
        "args": ["-y", "mcp-remote", "https://your-host/mcp/", "--header", "Authorization:${AUTH_HEADER}"],
        "env": { "AUTH_HEADER": "Bearer a-long-random-secret" }
      }
    }
  }
  ```

### Codex CLI

Add a Streamable-HTTP server to `~/.codex/config.toml`
([MCP docs](https://developers.openai.com/codex/mcp)). Codex reads the token from a **named env var**
(export `LABELITO_TOKEN` first):

```toml
[mcp_servers.labelito]
url = "https://your-host/mcp/"
bearer_token_env_var = "LABELITO_TOKEN"
```

Or set a static header directly with `[mcp_servers.labelito.http_headers]` → `Authorization = "Bearer
…"`. If an older Codex doesn't detect the HTTP server, add `experimental_use_rmcp_client = true` at
the top of the file or upgrade.

### curl smoke test

A quick JSON-RPC `initialize` to confirm the endpoint and token before wiring up a client:

```bash
curl -sS http://localhost:8765/mcp/ \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```
