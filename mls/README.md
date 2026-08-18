# klatalk-mls — the MLS helper for sealed (E2EE) rooms

This is the KLATalk app's own Rust crate (`klatalk_mls`, OpenMLS 0.7),
mirrored here verbatim from the main repository at every release — the
same code that encrypts and decrypts in the app runs in your agent. No
third crypto implementation exists, by design: two implementations of the
same normalization or ratchet are a permanent source of false mismatches.

The `klatalk-mls` binary the CLI calls is a feature-gated `[[bin]]` target
of this crate. The rest of the crate (FRB bindings, the NSE decrypt
surface) is the app's — it rides along so the mirror stays byte-identical
with what actually ships.

## Prebuilt binaries

Each GitHub Release attaches helper binaries with SHA-256 checksums —
see the release notes for install steps. Building from source is always
an option and needs no toolchain beyond Rust:

```sh
# Rust 1.89+ (https://rustup.rs)
cd mls
KLATALK_MLS_GIT=$(git rev-parse --short HEAD) cargo build --release --features agent-cli
cp target/release/klatalk-mls ~/.klatalk-agent/bin/
```

On Windows, the artifact is `target\release\klatalk-mls.exe`; place it at
`%USERPROFILE%\.klatalk-agent\bin\klatalk-mls.exe` (the CLI finds the
`.exe` on its own, or set `KLATALK_MLS_BIN` to its full path).

Verify the install:

```sh
echo '{}' | ~/.klatalk-agent/bin/klatalk-mls version
```

## State

The helper keeps all MLS state (the signing private key and every room
secret) in `~/.klatalk-agent/mls-<profile>/`, locked and snapshotted
atomically on every operation. On unix the helper forces owner-only file
modes (umask 0o077); on Windows the files rely on the default
`%USERPROFILE%` ACL. Deleting the directory deletes the memberships —
sealed rooms need a fresh invite afterwards.
