# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(the *Security → Report a vulnerability* tab of this repository). Include steps to reproduce
and the affected component. You'll get an acknowledgement, and fixes are coordinated before
public disclosure.

## Scope

In scope: the serving image, Kubernetes manifests, Terraform, CI/CD workflows and scripts in this
repository. Out of scope: vulnerabilities in upstream projects (TensorFlow Serving, GKE,
GitHub Actions); please report those upstream.

## Security design

- **No long-lived credentials.** CI/CD uses Workload Identity Federation, restricted to `main`
  and `v*` tags of one repository; pods mount no service-account token; nodes use the GKE
  metadata server.
- **Least privilege by default.** A dedicated node service account with only the GKE node role
  and pull access to one registry repository.
- **Network.** Private nodes; NetworkPolicy admits only the serving ports and **denies all
  egress**; only the REST port is exposed.
- **Workload.** Pod Security `restricted`; non-root UID; read-only root filesystem; all
  capabilities dropped; RuntimeDefault seccomp; Shielded VMs with secure boot.
- **Supply chain.** Immutable registry tags; base images pinned by version; CI actions pinned by
  commit SHA; Trivy scans in CI and before deploy; build provenance; Dependabot updates.

Known limitation: the endpoint has no authentication. See
[docs/EXTENSIONS.md](docs/EXTENSIONS.md#33-authentication-and-rate-limiting).
