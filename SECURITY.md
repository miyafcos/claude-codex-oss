# Security policy

This project is an unofficial developer source preview. It has not undergone a complete security audit. No response-time commitment is made.

## Scope and safe operation

Keep ccproxy bound to loopback. Do not expose the proxy through port forwarding, a public reverse proxy or a shared network interface. Provider authentication and service terms apply independently of this repository's license.

Without `-Apply`, `scripts/install.ps1` only displays proposed target paths and returns. With `-Apply`, it modifies user environment variables, launcher files, configuration and the separately installed ccproxy package. `-HomeDir` alone does not isolate every target. Installation may share existing Claude configuration through filesystem links. Review those links and use a separate Windows account or VM for evaluation.

Published settings use normal permission prompts and do not enable personal plugins. Existing user settings can retain different choices; the installer is not a security reset. Live installation, rollback and all provider paths have not been validated for this preview.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/miyafcos/claude-codex-oss/security/advisories/new) for security details. If the private form is unavailable, do not put those details in an issue: ask the maintainer for a private contact route without describing the vulnerability.

Do not post tokens, OAuth files, `.env` contents, real conversations, customer files or full proxy traces in public issues. Public bug reports must use synthetic examples and redacted metadata. Never send a working credential even in a private report.

## Scope of checks

Offline unit checks cover selected conversion helpers, model generation and public installer defaults. They are separate from authenticated live checks. Passing scans and unit tests does not establish that every dependency, configuration or integration is secure.
