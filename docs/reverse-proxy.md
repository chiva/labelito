# Reverse-proxy deployment

labelito serves plain HTTP on a single port (`8765`) and ships **no TLS of its own**. For anything
past a trusted LAN, front it with a **TLS-terminating reverse proxy** (Traefik, nginx, Caddy, Home
Assistant ingress, …). This page covers the two settings that make a proxied deployment behave —
forwarded-header trust and optional sub-path hosting — plus copy-paste examples per proxy.

- [Why the defaults break behind a proxy](#why-the-defaults-break-behind-a-proxy)
- [Trusting the proxy: `FORWARDED_ALLOW_IPS`](#trusting-the-proxy-forwarded_allow_ips)
- [Sub-path hosting: `PROXY_PATH_HEADER`](#sub-path-hosting-proxy_path_header)
- [Auth placement](#auth-placement)
- [The MCP endpoint behind a proxy](#the-mcp-endpoint-behind-a-proxy)
- [Examples](#examples)
  - [Traefik (Docker labels)](#traefik-docker-labels)
  - [nginx](#nginx)
  - [Caddy](#caddy)
- [Checklist](#checklist)

## Why the defaults break behind a proxy

A TLS-terminating proxy accepts `https` from the browser and forwards a **plain-HTTP** request to
labelito, advertising the original scheme/host/client in `X-Forwarded-Proto` / `-Host` / `-For`
headers. But the server (uvicorn) only honors those headers from **trusted** client IPs, and the
default trusted set is `127.0.0.1`. A containerized proxy connects from its container IP (e.g.
`172.18.0.3`), not localhost, so by default **the forwarded headers are ignored** and labelito
believes the request arrived over `http`.

The visible symptom: any redirect labelito emits is built with the wrong scheme. The most common one
is the MCP endpoint's `/mcp` → `/mcp/` **307**, whose `Location` comes back as
`http://your-host/mcp/`. Behind an `https` proxy that downgrade fails (or the client drops the auth
header across the hop). Generated absolute URLs (`/docs`, the OpenAPI `servers` entry) are affected
the same way.

## Trusting the proxy: `FORWARDED_ALLOW_IPS`

Set **`FORWARDED_ALLOW_IPS`** to the address labelito actually sees the proxy connect **from**, so it
honors that proxy's `X-Forwarded-*` headers:

| Value | Effect |
|---|---|
| *(unset — default `127.0.0.1`)* | Only a client whose **source IP is literally `127.0.0.1`** is trusted. See the caveat below — this is **not** the Docker case. |
| `10.0.0.5` | Trust a **single** proxy source IP. |
| `172.18.0.0/16` | Trust a **CIDR** — e.g. the Docker network your proxy and labelito share. |
| `172.18.0.3, 10.0.0.5` | Comma-separated list of IPs / CIDRs. |
| `*` | Trust **all** clients. Only safe when nothing but the proxy can open a connection to labelito (no published port). |

**What "connects from" means — the container caveat.** uvicorn trusts the **TCP peer IP** (the
source of the connection it terminates), *not* whatever is in `X-Forwarded-For`. That peer IP is
rarely `127.0.0.1` once containers are involved:

- **Proxy and labelito are separate containers on a shared Docker network** (the recommended setup):
  the peer IP is the **proxy container's** IP. Trust its subnet, e.g. `FORWARDED_ALLOW_IPS=172.18.0.0/16`
  (the network's CIDR).
- **Proxy on the host, labelito in a container with a published port** (`proxy_pass 127.0.0.1:8765`):
  Docker's NAT/userland-proxy **rewrites the source to the bridge gateway** (e.g. `172.17.0.1`), so
  from inside the container the client is *not* `127.0.0.1`. The default trust fails — set
  `FORWARDED_ALLOW_IPS` to that gateway IP (`docker network inspect bridge` → `Gateway`).
- **labelito runs as a bare host process** (`uv run uvicorn …`, no container) with the proxy also on
  the host over real loopback: then the peer genuinely is `127.0.0.1` and the default suffices.

Rules of thumb:

- **Scope it as tightly as you can.** Prefer the proxy's exact IP or its network CIDR over `*`.
- Only trust a proxy that **overwrites** `X-Forwarded-*` on every request (all examples below do). If
  a direct client can reach labelito from an address inside your trusted range, it can spoof its
  scheme/IP — so **don't publish `8765`** to any network you also trust here. The safest topology is
  the shared-network one above with **no published port** at all.
- To turn forwarded-header trust **off entirely**, run uvicorn with `--no-proxy-headers` (overriding
  the image's default `CMD`).

`FORWARDED_ALLOW_IPS` is read by uvicorn directly from the environment — the stock
`CMD ["uvicorn", …]` picks it up with no other change. Just add it to the container's environment.

> Finding the right value: `docker network inspect <network>` shows both the `Subnet` (use as the
> CIDR) and the `Gateway` (the source IP for host-published traffic). Prefer the specific subnet over
> the broad `172.16.0.0/12` private range.

## Sub-path hosting: `PROXY_PATH_HEADER`

Serving labelito under a **path prefix** (`https://host/labelito/` rather than its own hostname)?
Have the proxy send the prefix in a request header on every request and set **`PROXY_PATH_HEADER`**
to that header's name. labelito then uses the value as the base path for every generated URL (page
links, static assets, `/docs`, the OpenAPI `servers` entry) while route matching stays unchanged
(the proxy strips the prefix before forwarding). Home Assistant ingress sets `X-Ingress-Path` for
exactly this. Values not starting with `/` are ignored. If labelito owns a whole hostname/subdomain,
you don't need this.

## Auth placement

Pick one (details in [configuration.md](configuration.md#deployment--security-notes)):

- **Proxy does auth** — run labelito open with `ALLOW_UNAUTHENTICATED=true` and let the proxy gate
  access. Never expose `8765` directly; only the proxy should reach it.
- **App does auth** — keep `API_TOKEN` (bearer) or `WEB_AUTH_USER`/`WEB_AUTH_PASSWORD` (HTTP Basic)
  set; the proxy just terminates TLS and forwards. The `/mcp` endpoint reuses whichever you set.

Either way, cap the request body at the proxy (labelito rejects bodies over ~8 MiB with `413`, but a
public deployment should bound it upstream too).

## The MCP endpoint behind a proxy

Two things, both covered above:

1. Point MCP clients at the **trailing-slash** URL — `https://your-host/mcp/`, not `/mcp` — so there
   is no 307 to mishandle. See [docs/mcp.md](mcp.md#behind-a-reverse-proxy).
2. Set `FORWARDED_ALLOW_IPS` so the app knows it's on `https`.

## Examples

Each example terminates TLS at the proxy and forwards to labelito on `8765`, overwriting the
forwarded headers. Pair every one with `FORWARDED_ALLOW_IPS` set to the proxy's IP/CIDR on the
labelito container.

### Traefik (Docker labels)

```yaml
# docker-compose.yml — labelito service
services:
  labelito:
    image: ghcr.io/…/labelito:latest
    environment:
      API_TOKEN: ${API_TOKEN}
      # Trust Traefik's forwarded headers. Use YOUR shared Docker network's subnet
      # (`docker network inspect <network>` → Subnet), scoped as tightly as possible.
      FORWARDED_ALLOW_IPS: "172.18.0.0/16"
    # No published ports — reach it only through Traefik on the shared network
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.labelito.rule=Host(`labelito.local.example.com`)"
      - "traefik.http.routers.labelito.entrypoints=websecure"
      - "traefik.http.routers.labelito.tls=true"
      - "traefik.http.services.labelito.loadbalancer.server.port=8765"
```

Traefik forwards `X-Forwarded-Proto: https` by default. Because the container publishes no host
ports, the CIDR-scoped `FORWARDED_ALLOW_IPS` can't be abused from outside the Docker network.

### nginx

```nginx
server {
  listen 443 ssl;
  server_name labelito.example.com;
  # ssl_certificate / ssl_certificate_key …

  # Optional: bound request bodies upstream (app rejects >8 MiB with 413)
  client_max_body_size 8m;

  location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-Proto $scheme;   # https
    proxy_set_header X-Forwarded-For   $remote_addr;
    proxy_set_header X-Forwarded-Host  $host;
  }
}
```

Where to set `FORWARDED_ALLOW_IPS` depends on **how labelito runs** (see the container caveat above):
if labelito is a **bare host process**, host-nginx over `127.0.0.1` is trusted by default — no change
needed. If labelito is a **container** with `8765` published, nginx's connection arrives from the
Docker bridge gateway, so set `FORWARDED_ALLOW_IPS` to that gateway IP (e.g. `172.17.0.1`). Better,
run nginx as a container on the same network as labelito, drop the published port, and trust that
network's subnet.

### Caddy

```caddy
labelito.example.com {
  # Caddy provisions TLS automatically and sets X-Forwarded-* on the way through
  reverse_proxy 127.0.0.1:8765
}
```

Same `FORWARDED_ALLOW_IPS` rule as nginx: the default `127.0.0.1` only works when labelito is a bare
host process reached over real loopback; a containerized labelito needs the gateway IP or (better) the
shared-network subnet. To have Caddy also handle auth, add a `basic_auth` block and run labelito with
`ALLOW_UNAUTHENTICATED=true` (see [configuration.md](configuration.md#deployment--security-notes)).

## Checklist

- [ ] Proxy terminates TLS and forwards to labelito `:8765` over the internal network.
- [ ] Proxy sets/overwrites `X-Forwarded-Proto` (and `-Host`/`-For`) on every request.
- [ ] `FORWARDED_ALLOW_IPS` matches the **source IP labelito sees** the proxy connect from — the
      proxy's container subnet, or the Docker bridge gateway for a published port (only leave the
      `127.0.0.1` default when labelito is a bare host process over real loopback).
- [ ] Port `8765` is **not** published to any untrusted network.
- [ ] Sub-path hosting only: `PROXY_PATH_HEADER` set and the proxy sends that header every request.
- [ ] MCP clients use the **trailing-slash** URL `https://your-host/mcp/`.
- [ ] Request-body limit set at the proxy.
