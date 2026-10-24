# Third-party notices

## ccproxy-api

Upstream: https://github.com/CaddyGlow/ccproxy-api

`patches/ccproxy/` contains adapted code and additions for ccproxy-api. The patch manifest targets version 0.2.10. The upstream v0.2.10 source is identified by commit `9a0e49655f648f3eac7030a673674d1f8469893e`; its LICENSE is MIT, Copyright (c) 2025 CaddyGlow.

The upstream notice is retained in [LICENSES/ccproxy-api-MIT.txt](LICENSES/ccproxy-api-MIT.txt). Existing notices in individual files remain applicable. The root MIT license does not replace upstream attribution. Private upstream snapshots and installed distributions are not included in this repository.

The patches are modifications, not unmodified upstream files. A version number in the manifest is not a guarantee of compatibility with every installed package. Review and test the target installation before applying them.

## Separately installed products and dependencies

Claude Code, Codex CLI, uv, provider services and their credentials are not redistributed here. Their respective licenses, service terms and authentication requirements apply independently. No endorsement, partnership or trademark permission is implied.

Python development dependencies are declared in `pyproject.toml` and `uv.lock`; Rust dependencies are declared in `launchers/native/Cargo.toml` and `Cargo.lock`. These manifests do not bundle the dependency implementations or grant rights beyond their own licenses. Anyone distributing a built binary must review its included dependencies and retain the required notices. No compiled binary is distributed in this source preview.
