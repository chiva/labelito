# Security Policy

## Supported Versions

Only the latest release receives security fixes.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report security issues privately via
[GitHub Security Advisories](https://github.com/chiva/labelito/security/advisories/new)
or by emailing the maintainer directly.

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive a response within 72 hours. We will coordinate a fix and disclosure timeline with you.

## Scope

This service is designed for **local network (LAN) use only**. It is not designed to be
exposed to the public internet. Even with `API_TOKEN` enabled, do not expose port 8765
publicly without additional hardening (reverse proxy with TLS, firewall rules, VPN).
