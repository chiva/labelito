# SPDX-License-Identifier: GPL-3.0-or-later
"""OIDC Resource Server validation (app.oidc): JWT verdicts + RFC 9728 metadata helpers.

Hermetic — no live IdP. Tokens are minted with a local RSA keypair and the JWKS client is
monkeypatched to hand back the matching public key, so signature/claim validation runs for real.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.exceptions import PyJWKClientConnectionError

from app import oidc
from app.config import settings

_ISSUER = "https://idp.example/realms/labelito"
_AUDIENCE = "https://labelito.example/mcp"


@pytest.fixture
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _mint(key: rsa.RSAPrivateKey, claims: dict[str, Any], *, alg: str = "RS256") -> str:
    """Sign a JWT with `key`, defaulting the standard claims to a currently-valid token."""
    now = int(time.time())
    body = {"iss": _ISSUER, "aud": _AUDIENCE, "iat": now, "exp": now + 300, **claims}
    return jwt.encode(body, key, algorithm=alg, headers={"kid": "test-key"})


class _FakeSigningKey:
    def __init__(self, key: Any) -> None:
        self.key = key


class _FakeJWKSClient:
    """Stand-in for PyJWKClient: always returns `public_key`, or raises `exc` if given one."""

    def __init__(self, public_key: Any = None, exc: Exception | None = None) -> None:
        self._public_key = public_key
        self._exc = exc

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        if self._exc is not None:
            raise self._exc
        return _FakeSigningKey(self._public_key)


@pytest.fixture
def oidc_on(monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey) -> rsa.RSAPrivateKey:
    """Enable OIDC and wire the JWKS client to `rsa_key`'s public key."""
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", _ISSUER)
    monkeypatch.setattr(settings, "oidc_audience", _AUDIENCE)
    monkeypatch.setattr(settings, "oidc_required_scopes", None)
    monkeypatch.setattr(settings, "oidc_algorithms", "RS256")
    monkeypatch.setattr(settings, "oidc_leeway_seconds", 60)
    monkeypatch.setattr(oidc, "_get_jwks_client", lambda: _FakeJWKSClient(rsa_key.public_key()))
    return rsa_key


def test_valid_token(oidc_on: rsa.RSAPrivateKey) -> None:
    assert oidc.verify_bearer_token(_mint(oidc_on, {})) is oidc.Verdict.VALID


def test_bad_signature(oidc_on: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch) -> None:
    # Sign with a different key than the JWKS client hands back → signature check fails.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert oidc.verify_bearer_token(_mint(other, {})) is oidc.Verdict.INVALID


def test_wrong_audience(oidc_on: rsa.RSAPrivateKey) -> None:
    token = _mint(oidc_on, {"aud": "https://someone-else/mcp"})
    assert oidc.verify_bearer_token(token) is oidc.Verdict.INVALID


def test_wrong_issuer(oidc_on: rsa.RSAPrivateKey) -> None:
    token = _mint(oidc_on, {"iss": "https://evil.example"})
    assert oidc.verify_bearer_token(token) is oidc.Verdict.INVALID


def test_expired(oidc_on: rsa.RSAPrivateKey) -> None:
    now = int(time.time())
    token = _mint(oidc_on, {"exp": now - 120})  # beyond the 60s leeway
    assert oidc.verify_bearer_token(token) is oidc.Verdict.INVALID


def test_expired_within_leeway(oidc_on: rsa.RSAPrivateKey) -> None:
    now = int(time.time())
    token = _mint(oidc_on, {"exp": now - 30})  # within the 60s leeway → still valid
    assert oidc.verify_bearer_token(token) is oidc.Verdict.VALID


def test_missing_required_scope(
    oidc_on: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "oidc_required_scopes", "labelito.print labelito.read")
    token = _mint(oidc_on, {"scope": "labelito.read"})  # missing labelito.print
    assert oidc.verify_bearer_token(token) is oidc.Verdict.INSUFFICIENT_SCOPE


def test_sufficient_scope_from_scope_claim(
    oidc_on: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "oidc_required_scopes", "labelito.print")
    token = _mint(oidc_on, {"scope": "openid labelito.print"})
    assert oidc.verify_bearer_token(token) is oidc.Verdict.VALID


def test_sufficient_scope_from_scp_array(
    oidc_on: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "oidc_required_scopes", "labelito.print")
    token = _mint(oidc_on, {"scp": ["labelito.print", "labelito.read"]})
    assert oidc.verify_bearer_token(token) is oidc.Verdict.VALID


def test_jwks_unavailable_fails_closed(
    oidc_on: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    boom = _FakeJWKSClient(exc=PyJWKClientConnectionError("cannot reach IdP", "url"))
    monkeypatch.setattr(oidc, "_get_jwks_client", lambda: boom)
    assert oidc.verify_bearer_token(_mint(oidc_on, {})) is oidc.Verdict.UNAVAILABLE


def test_discovery_failure_fails_closed(
    oidc_on: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise() -> Any:
        raise oidc._OidcUnavailableError("discovery down")

    monkeypatch.setattr(oidc, "_get_jwks_client", _raise)
    assert oidc.verify_bearer_token(_mint(oidc_on, {})) is oidc.Verdict.UNAVAILABLE


def test_alg_none_rejected(oidc_on: rsa.RSAPrivateKey) -> None:
    # An unsigned token (alg=none) must never validate against the RS256 allowlist.
    now = int(time.time())
    token = jwt.encode(
        {"iss": _ISSUER, "aud": _AUDIENCE, "exp": now + 300},
        key=None,
        algorithm="none",
    )
    assert oidc.verify_bearer_token(token) is oidc.Verdict.INVALID


def test_disabled_returns_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_enabled", False)
    assert oidc.verify_bearer_token("anything") is oidc.Verdict.INVALID


# ── RFC 9728 metadata helpers ─────────────────────────────────────────────────────


def test_metadata_urls_plain() -> None:
    assert oidc.resource_url("https", "labelito.example", "") == "https://labelito.example/mcp"
    assert (
        oidc.resource_metadata_url("https", "labelito.example", "")
        == "https://labelito.example/.well-known/oauth-protected-resource/mcp"
    )


def test_metadata_urls_with_subpath() -> None:
    # A reverse-proxy sub-path (root_path) is reflected in the emitted URLs.
    assert (
        oidc.resource_url("https", "host.example", "/labelito")
        == "https://host.example/labelito/mcp"
    )
    assert (
        oidc.resource_metadata_url("https", "host.example", "/labelito/")
        == "https://host.example/labelito/.well-known/oauth-protected-resource/mcp"
    )


def test_metadata_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_issuer", _ISSUER)
    monkeypatch.setattr(settings, "oidc_required_scopes", "labelito.print")
    body = oidc.protected_resource_metadata("https", "labelito.example", "")
    assert body["resource"] == "https://labelito.example/mcp"
    assert body["authorization_servers"] == [_ISSUER]
    assert body["bearer_methods_supported"] == ["header"]
    assert body["scopes_supported"] == ["labelito.print"]


def test_metadata_body_omits_scopes_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_issuer", _ISSUER)
    monkeypatch.setattr(settings, "oidc_required_scopes", None)
    body = oidc.protected_resource_metadata("https", "labelito.example", "")
    assert "scopes_supported" not in body
