# MCP server

labelito can expose a [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server so an
AI client — Claude Desktop, an agent, or any MCP-capable app — can generate and print labels through
tools instead of raw HTTP calls. It reuses the exact same rendering, validation, printing,
idempotency, media pre-flight, and history logic as the REST API, so anything the tools do behaves
identically to the corresponding endpoint.

- [Enabling it](#enabling-it)
- [Transport & endpoint](#transport--endpoint)
- [Authentication](#authentication)
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
stateless, so a bare `POST /mcp` (which 307-redirects to `/mcp/`, followed automatically by clients)
carries a full JSON-RPC request. The full URL for a local deployment is
`http://localhost:8765/mcp`.

## Authentication

The `/mcp` endpoint is guarded by the **same credentials as the rest of the protected API**: a valid
`Authorization: Bearer <API_TOKEN>` **or** HTTP Basic (`WEB_AUTH_USER` / `WEB_AUTH_PASSWORD`). It
therefore inherits the fail-closed startup guard — the service refuses to start unless one auth mode
(or an explicit `ALLOW_UNAUTHENTICATED=true`) is configured. MCP clients typically send the bearer
token. In unauthenticated mode the endpoint is open, exactly like the rest of the API.

> DNS-rebinding Host/Origin validation is disabled on the `/mcp` mount (a self-hosted service is
> reached at an arbitrary, deployment-specific host/IP), so the bearer/Basic auth above plus network
> placement are the access controls — keep `API_TOKEN` set on any network-reachable deployment.

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

For an HTTP-capable MCP client, point it at the endpoint and add the bearer token, e.g. Claude
Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "labelito": {
      "url": "http://localhost:8765/mcp",
      "headers": { "Authorization": "Bearer a-long-random-secret" }
    }
  }
}
```

A quick smoke test with `curl` (JSON-RPC `initialize`):

```bash
curl -sS http://localhost:8765/mcp \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```
