## What and why

<!-- What does this change, and what problem does it solve? -->

## How it was tested

- [ ] `make lint test`
- [ ] `make test-integration` (if the model, image or client changed)
- [ ] Deployed to a test cluster / kind (if manifests or Terraform changed)
- [ ] Load test before/after (if performance or scaling settings changed): paste `summary.md` rows

## Checklist

- [ ] No environment values committed (project IDs, IPs, emails), placeholders in docs
- [ ] Design invariants in `tests/test_manifests.py` still hold, or are updated with a reason
- [ ] Docs updated (`README.md`, `docs/`)
