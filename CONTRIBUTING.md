# Contributing

This is a developer source preview. Use issues and pull requests for non-sensitive changes; follow SECURITY.md for vulnerabilities.

Keep changes focused and include a synthetic reproduction and tests. Do not include real credentials, user configuration, conversation traces or client data. Do not redistribute proprietary CLI binaries.

## Development checks

From the repository root, with development dependencies already available:

```powershell
python scripts/generate-model-configs.py --check
python -m pytest tests/ --ignore=tests/integration/ -q
cargo build --release --locked --offline --manifest-path launchers/native/Cargo.toml
```

These commands are not a substitute for checking test side effects. Keep test state separate from normal user state and do not authenticate or restart a shared proxy during unit tests.

`runtime/models.json` is the model source of truth. If a proposed change updates that file, regenerate the derived files with `python scripts/generate-model-configs.py --write` and review the complete diff. Do not silently change providers, aliases, effort, paid service tiers or permission behavior.

Installer tests must use a separately reviewed sandbox. `scripts/install.ps1 -HomeDir` alone does not isolate the uv tool package or user environment variables. Live tests, installations and service access require the operator's explicit approval.

Retain upstream notices and document the origin and license of any added code or asset. Contribution does not authorize use of a provider's trademarks or services outside their terms.
