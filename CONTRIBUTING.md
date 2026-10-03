# Contributing

## Development setup

```bash
make install            # .venv with client, test, load-test and lint tooling
make install-model      # TensorFlow, only needed to export the model or run model tests
make export-model build-local run-local
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for cloud deployment.

## Before opening a pull request

```bash
make format             # ruff + terraform fmt
make lint               # ruff, terraform fmt -check, kustomize render
make test               # unit + policy (+ model tests if TensorFlow is installed)
make test-integration   # against a running local container (make run-local)
```

CI runs the same checks, plus `terraform validate`, kubeconform, a from-source image build with
integration tests against the real server, and a Trivy scan. A PR must be green to merge.

## Guidelines

- **Measure before changing performance settings.** Use the Locust profiles
  (`make load-test`, see [docs/BENCHMARKS.md](docs/BENCHMARKS.md)) and include before/after
  numbers in the PR description.
- **Keep the design invariants.** `tests/test_manifests.py` encodes them: probes that don't depend
  on the REST thread pool, TF threads equal to the CPU limit, HPA bounds consistent with node
  capacity, a hard topology spread, no `replicas` in the Deployment, and pinned images. If you
  change an invariant deliberately, update the test and explain why.
- **Never commit environment values.** Project IDs, IPs and service-account emails belong in
  git-ignored files (`terraform/local.auto.tfvars`) or CI variables. Docs use `<PLACEHOLDERS>`.
- **No `:latest` images**: tags are immutable (`sha-<commit>`, `vX.Y.Z`).
- Small, focused commits with descriptive messages; one logical change per PR.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).

## Project layout

See the [repository layout](README.md#repository-layout) in the README.
