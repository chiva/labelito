# SPDX-License-Identifier: GPL-3.0-or-later
"""OAuth 2.0 Resource Server (OIDC) validation for the ``/mcp`` endpoint.

labelito is only a *Resource Server*: it validates bearer JWT access tokens minted by an operator's
EXTERNAL OpenID Connect provider (Keycloak/Authentik/Zitadel/…) and advertises that provider via RFC
9728 Protected Resource Metadata. Dynamic Client Registration (RFC 7591) and the OIDC login happen at
that provider — never here. This module is inert unless ``OIDC_ENABLED`` (see :mod:`app.config`).

Everything here is standalone so the single ``/mcp`` auth chokepoint in :mod:`app.main`
(``_mcp_authorized`` / ``_guard_mcp``) can accept a valid OIDC token OR the pre-existing static
``API_TOKEN`` bearer OR HTTP Basic — a plain boolean OR that can never drift from the REST gate.
"""

import enum
import json
import logging
import threading
import urllib.error
import urllib.request

import jwt
from jwt import PyJWKClient
from jwt.exceptions import (
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
)

from app.config import settings

log = logging.getLogger("labelito.oidc")

# Discovery/JWKS fetches must never stall the /mcp guard on a slow or dead IdP.
_DISCOVERY_TIMEOUT = 5.0  # seconds
# PyJWKClient caches signing keys this long and refetches on an unknown kid (handles rotation).
_JWKS_CACHE_LIFESPAN = 300  # seconds
_USER_AGENT = "labelito-oidc"

# Lazily built, lock-guarded singletons. Rebuilt only if the resolved JWKS URI changes.
_lock = threading.Lock()
_jwks_client: PyJWKClient | None = None
_jwks_client_uri: str | None = None
_resolved_jwks_uri: str | None = None


class Verdict(enum.Enum):
    """Outcome of validating a bearer JWT — maps to the guard's HTTP response.

    ``VALID`` → allow; ``INVALID`` → 401 ``invalid_token``; ``INSUFFICIENT_SCOPE`` → 403
    ``insufficient_scope``; ``UNAVAILABLE`` → 401 ``temporarily_unavailable`` (fail-closed: a JWKS or
    discovery fetch failure is never treated as a valid token).
    """

    VALID = "valid"
    INVALID = "invalid"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    UNAVAILABLE = "unavailable"


class _OidcUnavailableError(Exception):
    """OIDC discovery (openid-configuration) could not be fetched/parsed — a transient condition."""


def reset_cache() -> None:
    """Drop the cached JWKS client and resolved URI (used by tests; harmless in production)."""
    global _jwks_client, _jwks_client_uri, _resolved_jwks_uri
    with _lock:
        _jwks_client = None
        _jwks_client_uri = None
        _resolved_jwks_uri = None


def _resolve_jwks_uri() -> str:
    """Return the JWKS URI: the explicit ``OIDC_JWKS_URI`` if set, else via OIDC discovery (cached).

    Raises :class:`_OidcUnavailableError` when discovery is needed but the openid-configuration
    document cannot be fetched or lacks a usable ``jwks_uri`` — the caller maps that to a fail-closed
    ``UNAVAILABLE`` verdict rather than a 500.
    """
    global _resolved_jwks_uri
    if settings.oidc_jwks_uri:
        return settings.oidc_jwks_uri
    if not settings.oidc_discovery:
        raise _OidcUnavailableError(
            "OIDC_DISCOVERY is false but OIDC_JWKS_URI is unset — no way to locate signing keys"
        )
    if _resolved_jwks_uri is not None:
        return _resolved_jwks_uri
    issuer = (settings.oidc_issuer or "").rstrip("/")
    url = f"{issuer}/.well-known/openid-configuration"
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_DISCOVERY_TIMEOUT) as response:
            if response.status != 200:
                raise _OidcUnavailableError(f"discovery returned HTTP {response.status}")
            doc = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise _OidcUnavailableError(f"discovery fetch failed: {exc}") from exc
    jwks_uri = doc.get("jwks_uri") if isinstance(doc, dict) else None
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise _OidcUnavailableError("discovery document has no usable 'jwks_uri'")
    _resolved_jwks_uri = jwks_uri
    return jwks_uri


def _get_jwks_client() -> PyJWKClient:
    """Return a cached :class:`PyJWKClient` for the resolved JWKS URI, rebuilding if the URI changes.

    May raise :class:`_OidcUnavailableError` (via :func:`_resolve_jwks_uri`) when discovery fails.
    """
    global _jwks_client, _jwks_client_uri
    with _lock:
        uri = _resolve_jwks_uri()
        if _jwks_client is None or _jwks_client_uri != uri:
            _jwks_client = PyJWKClient(uri, cache_keys=True, lifespan=_JWKS_CACHE_LIFESPAN)
            _jwks_client_uri = uri
        return _jwks_client


def _scopes_ok(claims: dict[str, object]) -> bool:
    """True when the token carries every required scope (from ``scope`` string and/or ``scp`` array)."""
    required = settings.oidc_scopes_list
    if not required:
        return True
    granted: set[str] = set()
    scope = claims.get("scope")
    if isinstance(scope, str):
        granted.update(scope.split())
    scp = claims.get("scp")
    if isinstance(scp, str):
        granted.update(scp.split())
    elif isinstance(scp, list):
        granted.update(str(s) for s in scp)
    return all(s in granted for s in required)


def verify_bearer_token(token: str) -> Verdict:
    """Validate a bearer JWT access token and return a :class:`Verdict`.

    Verifies the signature against the IdP's JWKS and enforces ``iss``, ``aud``, ``exp`` (with
    ``OIDC_LEEWAY_SECONDS`` clock-skew tolerance) and the required scopes. The algorithm allowlist
    (``OIDC_ALGORITHMS``) structurally blocks ``alg:none`` and HMAC key-confusion. Never raises — a
    fetch failure fails closed to ``UNAVAILABLE`` and any bad/malformed token to ``INVALID``.
    """
    if not settings.oidc_configured:
        return Verdict.INVALID
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
    except (_OidcUnavailableError, PyJWKClientConnectionError) as exc:
        log.warning("OIDC: signing keys unavailable, denying (fail-closed): %s", exc)
        return Verdict.UNAVAILABLE
    except (PyJWKClientError, InvalidTokenError) as exc:
        # Unknown kid (after PyJWKClient's refetch) or a malformed token header — genuinely invalid.
        log.info("OIDC: no matching signing key / malformed token: %s", exc)
        return Verdict.INVALID
    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=settings.oidc_algorithms_list,
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            leeway=settings.oidc_leeway_seconds,
            options={"require": ["exp", "iss", "aud"]},
        )
    except InvalidTokenError as exc:
        log.info("OIDC: token rejected (%s)", exc)
        return Verdict.INVALID
    if not _scopes_ok(claims):
        log.info("OIDC: token missing required scope(s) %s", settings.oidc_scopes_list)
        return Verdict.INSUFFICIENT_SCOPE
    return Verdict.VALID


# ── RFC 9728 Protected Resource Metadata helpers ──────────────────────────────────────────────────
# The external URL is derived from the request's scheme/host (uvicorn rewrites these from the trusted
# X-Forwarded-* headers when FORWARDED_ALLOW_IPS is set) and root_path (populated from
# PROXY_PATH_HEADER by app.main._apply_proxy_root_path), so the advertised URLs match what the client
# actually reached — including behind a reverse proxy or under a sub-path.


def _external_base_url(scheme: str, host: str, root_path: str) -> str:
    """Public origin + optional sub-path prefix, e.g. ``https://host`` or ``https://host/labelito``."""
    return f"{scheme}://{host}{root_path.rstrip('/')}"


def resource_url(scheme: str, host: str, root_path: str) -> str:
    """Canonical resource identifier for the MCP endpoint (the ``resource`` value in the metadata)."""
    return f"{_external_base_url(scheme, host, root_path)}/mcp"


def resource_metadata_url(scheme: str, host: str, root_path: str) -> str:
    """RFC 9728 metadata URL for the ``/mcp`` resource (well-known prefix + the resource path)."""
    return f"{_external_base_url(scheme, host, root_path)}/.well-known/oauth-protected-resource/mcp"


def protected_resource_metadata(scheme: str, host: str, root_path: str) -> dict[str, object]:
    """Build the RFC 9728 Protected Resource Metadata document body."""
    metadata: dict[str, object] = {
        "resource": resource_url(scheme, host, root_path),
        "authorization_servers": [settings.oidc_issuer],
        "bearer_methods_supported": ["header"],
    }
    scopes = settings.oidc_scopes_list
    if scopes:
        metadata["scopes_supported"] = scopes
    return metadata
