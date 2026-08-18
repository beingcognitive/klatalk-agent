# klatalk-mls — the MLS helper for sealed (E2EE) rooms

> Sealed (E2EE/MLS) rooms are **experimental, but live-proven** from
> the agent CLI: sealed joins — quiz link, roster verification, two-way
> conversation — have been verified against the production server from
> the development environment (2026-08-14). This release is the first
> to distribute the helper beyond that environment, so external setups
> (Windows especially) are lightly traveled — expect rough edges, and
> reports are welcome.

This is the KLATalk app's own Rust crate (`klatalk_mls`, OpenMLS 0.7),
mirrored here verbatim from the main repository at every release. The
helper is a feature-gated `[[bin]]` target in the same crate: it drives
the same `MlsClient` core that seals and opens messages in the app, with
agent-only wrappers on top (epoch-CAS encryption, durable batch ingest).
No separate MLS implementation exists, by design: two implementations of
the same normalization or ratchet are a permanent source of false
mismatches. The rest of the crate (the app's Dart/FRB bindings, the
notification-extension surface) rides along so the mirror stays
identical with what actually ships.

Comments reference `§` sections and review markers (`/133`) from
KLATalk's internal design canon, which is not public — the protocol
itself is `docs/protocol-v1.md` territory. If a specific invariant's
rationale matters to you, open an issue and we'll publish the relevant
section.

## Prebuilt binaries

[Each GitHub Release](https://github.com/beingcognitive/klatalk-agent/releases)
attaches helper binaries with SHA-256 checksums — see the release notes
for install steps. Building from source is always
an option. It needs Rust 1.89+ **and a C compiler** — the crate's
Flutter-bridge dependency (`dart-sys`) compiles one vendored C file at
build time: Xcode Command Line Tools on macOS, `build-essential` (or
equivalent) on Linux, Visual Studio Build Tools (C++ workload) on
Windows.

```sh
# Rust 1.89+ (https://rustup.rs) plus the platform C toolchain
cd mls
mkdir -p ~/.klatalk-agent/bin
KLATALK_MLS_GIT=$(git describe --tags --always) cargo build --release --features agent-cli
cp target/release/klatalk-mls ~/.klatalk-agent/bin/
```

On Windows (PowerShell), the artifact keeps its `.exe`:

```powershell
cd mls
New-Item -Force -ItemType Directory "$env:USERPROFILE\.klatalk-agent\bin" | Out-Null
$env:KLATALK_MLS_GIT = (git describe --tags --always)
cargo build --release --features agent-cli
Copy-Item target\release\klatalk-mls.exe "$env:USERPROFILE\.klatalk-agent\bin\klatalk-mls.exe"
```

The CLI finds the `.exe` on its own, or set `KLATALK_MLS_BIN` to its
full path. Verify the install:

```sh
echo '{}' | ~/.klatalk-agent/bin/klatalk-mls version
```

The `git` field names the source this binary was built from: release
binaries are stamped with the public release tag; a self-build stamped
as above reports the tag or commit of your checkout.

## State

The helper keeps all MLS state (the signing private key and every room
secret) in `~/.klatalk-agent/mls-<profile>/`, locked and snapshotted
atomically on every operation. On unix the helper forces owner-only file
modes (umask 0o077); on Windows nothing equivalent is enforced — the
files inherit the ACL of the directory they live in, so keep it under
`%USERPROFILE%` (whose default ACL still includes SYSTEM and
Administrators). Deleting the directory deletes the memberships —
sealed rooms need a fresh invite afterwards.
