//! klatalk-mls — the agent helper binary (agent-mls-v1 §1).
//!
//! A one-shot vault-keeper the Python CLI calls as a subprocess. The very
//! same crate code as the app runs here (no third crypto implementation,
//! §0-1).
//!
//! Contract:
//! - Input: `klatalk-mls <op> --dir DIR [--identity ID]` + **one line of
//!   stdin JSON** — sensitive arguments (plaintexts, keys, answers) never
//!   ride argv (ps exposure).
//! - Output: one line of stdout JSON. Failures are {"error": …} + exit 1.
//! - All binary values are base64 (`_b64` suffix).
//! - The external-init rejection is **always on at runtime** — enabled on
//!   every open regardless of build env. No op can turn it off (/133
//!   verdict: a policy that can be turned off is not a door).
//! - umask 0o077 as the first statement of main, before any file is
//!   created — **unix only**. On Windows there is no umask and neither
//!   this binary nor the crate applies permission hardening: the state
//!   dir inherits the ACL of whatever path `--dir` names (keep it under
//!   %USERPROFILE%, whose default ACL still includes SYSTEM and
//!   Administrators).

use std::io::Read;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use klatalk_mls::api::mls::{Incoming, IncomingKind, MemberInfo, MlsClient};
use klatalk_mls::api::quiz;
use serde_json::{json, Value};

fn main() {
    // The state file holds the signing private key and every room secret.
    // umask is unix-only — libc itself is a unix-target dependency, so an
    // unguarded call does not compile on Windows (win CI E0433,
    // 2026-08-18). On Windows NO hardening is applied here: state files
    // inherit the ACL of the directory --dir points at
    #[cfg(unix)]
    unsafe {
        libc::umask(0o077);
    }

    match run() {
        Ok(v) => println!("{v}"),
        Err(e) => {
            println!("{}", json!({ "error": format!("{e:#}") }));
            std::process::exit(1);
        }
    }
}

fn run() -> anyhow::Result<Value> {
    let args: Vec<String> = std::env::args().collect();
    let op = args.get(1).map(String::as_str).unwrap_or("");

    let flag = |name: &str| -> Option<String> {
        args.iter()
            .position(|a| a == name)
            .and_then(|i| args.get(i + 1))
            .cloned()
    };

    // stdin JSON — some ops take no input, so empty input reads as an
    // empty object
    let mut raw = String::new();
    std::io::stdin().read_to_string(&mut raw)?;
    let input: Value = if raw.trim().is_empty() {
        json!({})
    } else {
        serde_json::from_str(raw.trim())?
    };

    let b64_field = |key: &str| -> anyhow::Result<Vec<u8>> {
        let s = input
            .get(key)
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow::anyhow!("missing field: {key}"))?;
        Ok(B64.decode(s)?)
    };
    let str_field = |key: &str| -> anyhow::Result<String> {
        Ok(input
            .get(key)
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow::anyhow!("missing field: {key}"))?
            .to_string())
    };
    // group_id = the UTF-8 bytes of the room_id string (same as the app's
    // _gid — not the raw UUID)
    let gid = |key: &str| -> anyhow::Result<Vec<u8>> {
        Ok(str_field(key)?.into_bytes())
    };

    // Stateless ops first
    match op {
        "version" => {
            return Ok(json!({
                "version": env!("CARGO_PKG_VERSION"),
                "crate": "klatalk_mls",
                "git": option_env!("KLATALK_MLS_GIT").unwrap_or("unknown"),
            }));
        }
        "quiz-seal" => {
            // The answer arrives via stdin JSON only — same code as the
            // app all the way through normalize
            let pk = b64_field("public_key_b64")?;
            let aad = b64_field("aad_b64")?;
            let normalized = quiz::quiz_normalize(str_field("answer")?);
            let sealed = quiz::quiz_seal(pk, aad, normalized.into_bytes())?;
            return Ok(json!({ "sealed_b64": B64.encode(sealed) }));
        }
        _ => {}
    }

    let dir = flag("--dir").ok_or_else(|| anyhow::anyhow!("--dir required"))?;
    let identity = flag("--identity").ok_or_else(|| anyhow::anyhow!("--identity required"))?;

    // Create the dir here so it picks up the entry umask (unix only) —
    // both this and the crate's create_dir_all lean on umask alone, and
    // neither re-chmods a pre-existing dir. No effect on Windows
    std::fs::create_dir_all(&dir)?;

    let mut client = MlsClient::open(dir, identity)?;
    // Always on at runtime — the moment a forged ExternalInit commit gets
    // merged, we become a weaker member than the app (§8-3 gate asymmetry,
    // /133 5/6)
    client.set_reject_external_init(true);

    match op {
        "open" => Ok(json!({ "ok": true })),

        "signing-public-key" => Ok(json!({
            "public_key_b64": B64.encode(client.signing_public_key()?)
        })),

        "sign" => Ok(json!({
            "signature_b64": B64.encode(client.sign(b64_field("message_b64")?)?)
        })),

        "create-key-package" => Ok(json!({
            "key_package_b64": B64.encode(client.create_key_package()?)
        })),

        "join-group" => {
            // The §3-5 guard lives inside the helper — a Welcome grafting
            // onto the wrong room deletes the group and fails (same as the
            // app's joinFromWelcome). request_id persists in the same
            // transaction as the group, preventing stale-group mistaken
            // identity (impl /133 3/6)
            let expected = gid("expected_room_id")?;
            let request_id = str_field("request_id")?;
            let group_id = client.agent_join_group(b64_field("welcome_b64")?, request_id)?;
            if group_id != expected {
                client.delete_group(group_id)?;
                anyhow::bail!("wrong_room");
            }
            Ok(json!({ "room_id": String::from_utf8_lossy(&group_id) }))
        }

        "ingest" => {
            let msgs = input
                .get("messages")
                .and_then(Value::as_array)
                .ok_or_else(|| anyhow::anyhow!("missing field: messages"))?;
            let mut items = Vec::with_capacity(msgs.len());
            for m in msgs {
                let seq = m
                    .get("seq")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| anyhow::anyhow!("message missing seq"))?;
                let ct = B64.decode(
                    m.get("ct_b64")
                        .and_then(Value::as_str)
                        .ok_or_else(|| anyhow::anyhow!("message missing ct_b64"))?,
                )?;
                items.push((seq, ct));
            }
            let outcomes = client.agent_ingest(gid("room_id")?, items)?;
            let receipts: Vec<Value> = outcomes
                .iter()
                .map(|o| {
                    let mut v = json!({
                        "seq": o.seq,
                        "replayed": o.replayed,
                        "pruned": o.pruned,
                    });
                    if let Some(inc) = &o.incoming {
                        v["incoming"] = incoming_json(inc);
                    }
                    if let Some(err) = &o.error {
                        v["error"] = json!(err);
                    }
                    v
                })
                .collect();
            Ok(json!({ "receipts": receipts }))
        }

        "ingest-skip" => {
            let seq = input
                .get("seq")
                .and_then(Value::as_u64)
                .ok_or_else(|| anyhow::anyhow!("missing field: seq"))?;
            client.agent_ingest_skip(gid("room_id")?, seq)?;
            Ok(json!({ "ok": true }))
        }

        "ingest-ack" => {
            let upto = input
                .get("upto_seq")
                .and_then(Value::as_u64)
                .ok_or_else(|| anyhow::anyhow!("missing field: upto_seq"))?;
            let removed = client.agent_ingest_ack(gid("room_id")?, upto)?;
            Ok(json!({ "removed": removed }))
        }

        "cursor" => {
            let (cursor, resume) = client.agent_cursor(gid("room_id")?)?;
            Ok(json!({ "cursor": cursor, "resume": resume }))
        }

        "encrypt" => {
            // epoch CAS — only lock when the roster matches the one we
            // verified against (/133 P0)
            let epoch = input
                .get("expected_epoch")
                .and_then(Value::as_u64)
                .ok_or_else(|| anyhow::anyhow!("missing field: expected_epoch"))?;
            Ok(json!({
                "ct_b64": B64.encode(client.agent_encrypt(
                    gid("room_id")?, epoch, b64_field("plaintext_b64")?)?)
            }))
        }

        "has-group" => {
            let g = gid("room_id")?;
            let has = client.has_group(g.clone())?;
            let receipt = if has { client.agent_join_receipt(g)? } else { None };
            Ok(json!({ "has_group": has, "join_request_id": receipt }))
        }

        "group-epoch" => Ok(json!({
            "epoch": client.group_epoch(gid("room_id")?)?
        })),

        "list-members" => Ok(json!({
            "members": client
                .list_members(gid("room_id")?)?
                .iter()
                .map(member_json)
                .collect::<Vec<_>>()
        })),

        "delete-group" => {
            client.delete_group(gid("room_id")?)?;
            Ok(json!({ "ok": true }))
        }

        other => anyhow::bail!("unknown op: {other}"),
    }
}

fn member_json(m: &MemberInfo) -> Value {
    json!({
        "leaf_index": m.leaf_index,
        "identity": m.identity,
        "signature_key_b64": B64.encode(&m.signature_key),
    })
}

fn incoming_json(inc: &Incoming) -> Value {
    json!({
        "kind": match inc.kind {
            IncomingKind::Application => "application",
            IncomingKind::Handshake => "handshake",
            IncomingKind::Own => "own",
            IncomingKind::RejectedExternalJoin => "rejected_external_join",
        },
        "sender": inc.sender,
        "plaintext_b64": inc.plaintext.as_ref().map(|p| B64.encode(p)),
        "epoch": inc.epoch,
        "added": inc.added.iter().map(member_json).collect::<Vec<_>>(),
        "removed": inc.removed.iter().map(member_json).collect::<Vec<_>>(),
    })
}
