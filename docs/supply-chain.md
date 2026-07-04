# Supply-chain security & verifying images

Every released `ghcr.io/chiva/labelito` image is signed and carries provenance, so you can prove
an image you pulled was built from this repository by its official release pipeline — not swapped
out by someone with registry access. This is produced automatically on release by the `docker`
job in [`.github/workflows/release-please.yml`](../.github/workflows/release-please.yml).

## What each image carries

| Artifact | Signed / logged | What it proves | Verify with |
| --- | --- | --- | --- |
| **Build provenance attestation** | Sigstore (Fulcio + Rekor) | The image digest was built by *this* GitHub workflow, from a specific commit/tag. | `gh attestation verify` |
| **cosign signature** | Sigstore (Fulcio + Rekor) | The image digest was signed by *this* workflow's identity. | `cosign verify` |
| BuildKit provenance + SBOM | Embedded, **unsigned** | Build metadata and the software bill of materials. | `docker buildx imagetools inspect` |

All signing is **keyless**: the GitHub Actions OIDC token is the signing identity, so there is no
private key stored in the repository or in secrets. Signatures and attestations are recorded in the
public [Rekor](https://docs.sigstore.dev/logging/overview/) transparency log.

## What signing does — and does not — protect against

- **Protects:** an image swapped or re-tagged by someone with GHCR write access (a leaked package
  token, or a registry-side compromise). You no longer have to trust the registry, only the
  transparency log and the workflow identity.
- **Does not protect:** a compromised repository or CI pipeline. Keyless signing binds to the
  *workflow identity*, so anything the official workflow builds — including a malicious commit that
  reached `main` — gets a valid signature. Signing proves origin, not benignity.

## Verify a pulled image

Both commands verify by **digest**, and both require the expected signer identity — the workflow
file and this repository — so a signature made by a *different* repo does not pass.

### Build provenance (GitHub CLI)

```bash
gh attestation verify oci://ghcr.io/chiva/labelito:<tag> \
  --repo chiva/labelito
```

### cosign signature

```bash
cosign verify ghcr.io/chiva/labelito:<tag> \
  --certificate-identity-regexp '^https://github.com/chiva/labelito/\.github/workflows/release-please\.yml@.*$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

A successful run prints the certificate subject (the workflow identity) and the Rekor log entry.
A non-zero exit means the image is unsigned, tampered with, or signed by an unexpected identity —
do not run it.

## Enforce it in Kubernetes (optional)

If you deploy labelito into a cluster, admission controllers such as
[Kyverno](https://kyverno.io/policies/) or the
[sigstore policy-controller](https://docs.sigstore.dev/policy-controller/overview/) can **reject**
any `ghcr.io/chiva/labelito` image that is not signed by the identity above, turning verification
from a manual step into a cluster-wide gate.
