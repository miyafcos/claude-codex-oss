# Native Windows launcher

This optional Rust executable accelerates eligible Windows launches. The PowerShell launcher remains the fallback for unsupported arguments, settings or script hashes.

From the repository root, with Rust, Windows build tools and the locked dependencies already cached:

```powershell
cargo build --release --locked --offline --manifest-path launchers/native/Cargo.toml
```

The installable binary is `launchers/native/target/release/claude-codex-native.exe`. `launcher-test-child.exe` is a test fixture and must not be installed. No prebuilt binary is included in this source preview.

The installed native executable requires the colocated `claude-codex.ps1`. An unknown normalized script digest selects the PowerShell fallback. `CLAUDE_CODEX_NATIVE=0` forces that fallback. Review the digests and parity tests when changing the PowerShell launcher; do not approve a new digest merely to make the native path run.

Eligibility checks precede directory creation, proxy startup and launching Claude. Native and PowerShell paths share a maintenance mutex but use separate fast-path caches. Simultaneously cold paths can each request a start; this is not evidence that two healthy proxies are running. Stopping only the launcher may leave its child running.

## Diagnostics

The executable must be beside its script. `probe` inspects inputs without writes, child processes or HTTP. `plan` may perform a read-only GET of the local model endpoint; it does not start the proxy or Claude and does not save a proposed mapping.

```powershell
$env:CLAUDE_CODEX_NATIVE_SELFTEST = 'probe'
.\claude-codex-native.exe
$env:CLAUDE_CODEX_NATIVE_SELFTEST = $null
```

Do not publish diagnostic output containing private paths, account data or configuration. Use a separate test profile and synthetic fixtures for changes to startup behavior. The source preview's offline checks are not a complete native-versus-PowerShell parity or live-account test.
