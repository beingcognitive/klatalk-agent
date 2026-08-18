//! MLS client — an OpenMLS wrapper (docs/e2ee-mls-v1.md Phase 3).
//!
//! Design principle: all I/O is bytes; the server is a warehouse + postman
//! that never parses. From the Phase 1 spike (in-memory, Add/Welcome) to
//! production grade:
//!
//! - **File persistence**: identity (signing keypair) + the entire OpenMLS
//!   storage snapshot in a single state file. Every public operation runs
//!   flock (lock) → reload → operate → save, so the app and the NSE
//!   (notification extension) can share one state file without tearing.
//!   Even if both processes each decrypt the same message, state converges
//!   to the same place.
//! - **external commit**: the join path for invite-code joins and device
//!   linking (design decisions 1·2). The server's GroupInfo is the ticket,
//!   and the produced commit is relayed on the seq stream.
//! - **Unified receive handling**: application/commit/proposal all go
//!   through a single `process_incoming`.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
#[cfg(unix)]
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Context, Result};
use openmls::prelude::{tls_codec::*, *};
use openmls_basic_credential::SignatureKeyPair;
use openmls_rust_crypto::OpenMlsRustCrypto;

const CIPHERSUITE: Ciphersuite = Ciphersuite::MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519;
const STATE_MAGIC: &[u8; 5] = b"KMLS1";
const STATE_FILE: &str = "mls_state.bin";
const LOCK_FILE: &str = "mls_state.lock";

/// **Compile-time default** of the ExternalInit rejection gate — the §8-4
/// cutover build flips it with `KLATALK_REJECT_EXTERNAL_JOINS=1`. The app
/// and the NSE link the same library, so this is the single policy source
/// for both processes: the NSE cannot read Dart --dart-define, and a
/// marker file cannot block notifications between an update and the app's
/// first launch (Codex §8-3 P1 — closing the gate asymmetry).
const REJECT_EXTERNAL_INIT_DEFAULT: bool = match option_env!("KLATALK_REJECT_EXTERNAL_JOINS") {
    Some(v) => matches!(v.as_bytes(), b"1" | b"true"),
    None => false,
};

/// The products of an Add commit — all bytes relayed to the server as-is.
pub struct AddOutcome {
    /// The commit for existing members (Envelope v2, hint:"none")
    pub commit: Vec<u8>,
    /// The Welcome for the new member
    pub welcome: Vec<u8>,
    /// The new GroupInfo after the commit — PUT to the server (epoch advance)
    pub group_info: Vec<u8>,
    /// The epoch after the commit — used for the GroupInfo PUT
    pub epoch: u64,
}

/// The products of an external commit join.
pub struct ExternalJoinOutcome {
    /// The joined room (group) id — our room UUID bytes
    pub group_id: Vec<u8>,
    /// The commit for existing members (Envelope v2, hint:"none")
    pub commit: Vec<u8>,
    /// The new GroupInfo after joining — PUT to the server
    pub group_info: Vec<u8>,
    /// The epoch after joining
    pub epoch: u64,
}

/// The products of a Remove commit — an AddOutcome without a Welcome
/// (Phase A §8-3).
pub struct RemoveOutcome {
    /// The commit for the remaining members (Envelope v2, hint:"none")
    pub commit: Vec<u8>,
    /// The new GroupInfo after the commit — PUT to the server
    pub group_info: Vec<u8>,
    /// The epoch after the commit
    pub epoch: u64,
}

/// A summary of one member leaf — the material for AS registry comparison
/// (spec §4) and the approving device's stale-leaf check before an Add.
/// identity is the BasicCredential identity — device_id in our convention.
#[derive(Clone)]
pub struct MemberInfo {
    pub leaf_index: u32,
    pub identity: String,
    /// The leaf's signature public key — compared against the server
    /// registry (signing key)
    pub signature_key: Vec<u8>,
}

/// Kinds of incoming messages. (A fieldless enum — FRB maps it without
/// freezed)
pub enum IncomingKind {
    /// A plaintext message to show the user — sender·plaintext are filled
    Application,
    /// A commit/proposal was handled — group state advanced, epoch is filled
    Handshake,
    /// An echo of a message this device sent — use the local original
    Own,
    /// An external join (ExternalInit) commit rejected by policy — group
    /// state did NOT advance (§8-3 gate, enabled at the §8-4 cutover)
    RejectedExternalJoin,
}

/// The result of processing an incoming message. added/removed are filled
/// only for handshakes (commits) — a before/after member diff (keyed by
/// signature key), so it catches Add/Remove/external joins alike. The AS
/// (§4) compares added's (identity, signature_key) against the registry.
pub struct Incoming {
    pub kind: IncomingKind,
    pub sender: Option<String>,
    pub plaintext: Option<Vec<u8>>,
    pub epoch: Option<u64>,
    pub added: Vec<MemberInfo>,
    pub removed: Vec<MemberInfo>,
}

impl Incoming {
    fn application(sender: String, plaintext: Vec<u8>) -> Incoming {
        Incoming {
            kind: IncomingKind::Application,
            sender: Some(sender),
            plaintext: Some(plaintext),
            epoch: None,
            added: Vec::new(),
            removed: Vec::new(),
        }
    }

    fn handshake(epoch: u64, added: Vec<MemberInfo>, removed: Vec<MemberInfo>) -> Incoming {
        Incoming {
            kind: IncomingKind::Handshake,
            sender: None,
            plaintext: None,
            epoch: Some(epoch),
            added,
            removed,
        }
    }

    fn own() -> Incoming {
        Incoming {
            kind: IncomingKind::Own,
            sender: None,
            plaintext: None,
            epoch: None,
            added: Vec::new(),
            removed: Vec::new(),
        }
    }

    fn rejected_external_join() -> Incoming {
        Incoming {
            kind: IncomingKind::RejectedExternalJoin,
            sender: None,
            plaintext: None,
            epoch: None,
            added: Vec::new(),
            removed: Vec::new(),
        }
    }
}

/// One device = one MLS client. FRB wraps it opaque; Dart only holds a
/// handle.
pub struct MlsClient {
    dir: PathBuf,
    identity: String,
    provider: OpenMlsRustCrypto,
    signer: SignatureKeyPair,
    credential_with_key: CredentialWithKey,
    /// The §8-4 cutover gate — when on, ExternalInit (external join)
    /// commits are rejected. Not persisted in the state file: policy is
    /// decided by the app version (default off = dark).
    reject_external_init: bool,
}

impl MlsClient {
    /// Opens or initializes the state directory. identity is fixed per
    /// device (e.g. device_id) — mismatching the file's identity is an
    /// error.
    pub fn open(dir: String, identity: String) -> Result<MlsClient> {
        let dir = PathBuf::from(dir);
        fs::create_dir_all(&dir).context("create state dir")?;
        let _lock = FileLock::exclusive(&dir.join(LOCK_FILE))?;

        let state_path = dir.join(STATE_FILE);

        if state_path.exists() {
            let state = StateFile::read(&state_path)?;
            if state.identity != identity {
                bail!("identity mismatch: state file belongs to another device");
            }
            Self::from_state(dir, state)
        } else {
            let provider = OpenMlsRustCrypto::default();
            let signer = SignatureKeyPair::new(CIPHERSUITE.signature_algorithm())
                .map_err(|e| anyhow!("signature keypair: {e:?}"))?;
            signer
                .store(provider.storage())
                .map_err(|e| anyhow!("signer store: {e:?}"))?;

            let credential = BasicCredential::new(identity.clone().into_bytes());
            let client = MlsClient {
                dir,
                identity,
                credential_with_key: CredentialWithKey {
                    credential: credential.into(),
                    signature_key: signer.to_public_vec().into(),
                },
                provider,
                signer,
                reject_external_init: REJECT_EXTERNAL_INIT_DEFAULT,
            };
            client.save()?;
            Ok(client)
        }
    }

    /// Opens an already-initialized state directory without the identity
    /// check — errors when the state file is missing. For a second process
    /// that does not know the identity, like the NSE.
    pub fn open_existing(dir: String) -> Result<MlsClient> {
        let dir = PathBuf::from(dir);
        let _lock = FileLock::exclusive(&dir.join(LOCK_FILE))?;
        let state = StateFile::read(&dir.join(STATE_FILE))?;
        Self::from_state(dir, state)
    }

    /// Opens with a sealed signing keypair — reinstall self-healing (rekey
    /// §7 prevention track). If a state file exists the sealed argument is
    /// ignored (the state file is canon — the caller converges the keychain
    /// to it via export after open). If not, the sealed keypair is restored
    /// instead of generating a new key: it matches the registry's existing
    /// key, so the existing bootstrap re-joins without rekey or warnings.
    /// Group state is never sealed — as long as the identity key survives,
    /// bootstrap fills the rest.
    pub fn open_sealed(dir: String, identity: String, sealed_signer: Vec<u8>) -> Result<MlsClient> {
        let dir = PathBuf::from(dir);
        fs::create_dir_all(&dir).context("create state dir")?;
        let _lock = FileLock::exclusive(&dir.join(LOCK_FILE))?;

        let state_path = dir.join(STATE_FILE);
        if state_path.exists() {
            let state = StateFile::read(&state_path)?;
            if state.identity != identity {
                bail!("identity mismatch: state file belongs to another device");
            }
            return Self::from_state(dir, state);
        }

        let provider = OpenMlsRustCrypto::default();
        let signer = SignatureKeyPair::tls_deserialize_exact(sealed_signer.as_slice())
            .map_err(|e| anyhow!("sealed signer deserialize: {e:?}"))?;
        signer
            .store(provider.storage())
            .map_err(|e| anyhow!("signer store: {e:?}"))?;

        let credential = BasicCredential::new(identity.clone().into_bytes());
        let client = MlsClient {
            dir,
            identity,
            credential_with_key: CredentialWithKey {
                credential: credential.into(),
                signature_key: signer.to_public_vec().into(),
            },
            provider,
            signer,
            reject_external_init: REJECT_EXTERNAL_INIT_DEFAULT,
        };
        client.save()?;
        Ok(client)
    }

    /// Exports the whole signing keypair (tls_codec) — the material for
    /// keychain sealing (§7). Same grade as the bytes already in the state
    /// file, and the keychain is the stronger vault.
    pub fn export_signing_keypair(&mut self) -> Result<Vec<u8>> {
        self.locked(|c| {
            c.signer
                .tls_serialize_detached()
                .map_err(|e| anyhow!("signer serialize: {e:?}"))
        })
    }

    /// This device's signature public key (Ed25519, 32 bytes) — the value
    /// registered with the AS (§2). It is the same key as the KeyPackage
    /// leaf signature key, which is what makes the approver's comparison
    /// work.
    pub fn signing_public_key(&mut self) -> Result<Vec<u8>> {
        self.locked(|c| Ok(c.signer.to_public_vec()))
    }

    /// Signs arbitrary bytes with this device's signing key (rekey §3
    /// corollary 2) — the signature material for rekey approval
    /// challenges, link-code challenges, and token revocation. The private
    /// key never leaves the device, so a session hijacker cannot imitate
    /// it. Ed25519 raw sign (64 bytes).
    pub fn sign(&mut self, message: Vec<u8>) -> Result<Vec<u8>> {
        self.locked(|c| {
            openmls_traits::signatures::Signer::sign(&c.signer, &message)
                .map_err(|e| anyhow!("sign: {e:?}"))
        })
    }

    /// Inspects KeyPackage bytes — the approver's verification material
    /// (§3-4 ①): extracts the identity (device_id) and the leaf signature
    /// public key for registry comparison. An invalid KeyPackage is an
    /// error (validation failure = a reject reason).
    pub fn key_package_info(&mut self, key_package: Vec<u8>) -> Result<MemberInfo> {
        self.locked(|c| {
            let kp_in = KeyPackageIn::tls_deserialize_exact(key_package.as_slice())
                .context("key package deserialize")?;
            let kp = kp_in
                .validate(c.provider.crypto(), ProtocolVersion::Mls10)
                .map_err(|e| anyhow!("key package validate: {e:?}"))?;

            let leaf = kp.leaf_node();

            Ok(MemberInfo {
                // A leaf not yet in any group — the index is meaningless
                leaf_index: u32::MAX,
                identity: basic_identity(leaf.credential()),
                signature_key: leaf.signature_key().as_slice().to_vec(),
            })
        })
    }

    /// Creates and serializes a KeyPackage — bytes pre-uploaded to the
    /// server directory. The private key material stays in storage for use
    /// when the Welcome arrives.
    pub fn create_key_package(&mut self) -> Result<Vec<u8>> {
        self.locked(|c| {
            let bundle = KeyPackage::builder()
                .build(
                    CIPHERSUITE,
                    &c.provider,
                    &c.signer,
                    c.credential_with_key.clone(),
                )
                .map_err(|e| anyhow!("key package: {e:?}"))?;

            bundle
                .key_package()
                .tls_serialize_detached()
                .context("key package serialize")
        })
    }

    /// Creates a room — group_id is our room UUID bytes. Returns the
    /// signed GroupInfo (PUT to the server as the ticket for external
    /// commit joins).
    pub fn create_group(&mut self, group_id: Vec<u8>) -> Result<Vec<u8>> {
        self.locked(|c| {
            let group = MlsGroup::new_with_group_id(
                &c.provider,
                &c.signer,
                &group_config(),
                GroupId::from_slice(&group_id),
                c.credential_with_key.clone(),
            )
            .map_err(|e| anyhow!("create group: {e:?}"))?;

            c.export_group_info_for(&group)
        })
    }

    /// The current GroupInfo of this group (signed, ratchet tree included).
    pub fn export_group_info(&mut self, group_id: Vec<u8>) -> Result<Vec<u8>> {
        self.locked(|c| {
            let group = c.load_group(&group_id)?;
            c.export_group_info_for(&group)
        })
    }

    /// External commit join using the server's GroupInfo bytes — both
    /// invite-code joins and device linking take this path. The commit is
    /// relayed as a hint:"none" message, and the new group_info is PUT to
    /// the server (409 means another commit won — re-join).
    pub fn join_by_external_commit(&mut self, group_info: Vec<u8>) -> Result<ExternalJoinOutcome> {
        self.locked(|c| {
            let msg = MlsMessageIn::tls_deserialize_exact(group_info.as_slice())
                .context("group info deserialize")?;

            let verifiable_group_info = match msg.extract() {
                MlsMessageBodyIn::GroupInfo(gi) => gi,
                _ => bail!("not a group info message"),
            };

            // Deprecation warning suppressed — the external path itself is
            // removed at §8-4. We do not port dying code to the new builder
            // API (mls-phase-a-v1 §8-3)
            #[allow(deprecated)]
            let (group, commit, _group_info) = MlsGroup::join_by_external_commit(
                &c.provider,
                &c.signer,
                None, // the ratchet tree rides the GroupInfo extension
                verifiable_group_info,
                group_config().join_config(),
                None,
                None,
                &[],
                c.credential_with_key.clone(),
            )
            .map_err(|e| anyhow!("external commit: {e:?}"))?;

            Ok(ExternalJoinOutcome {
                group_id: group.group_id().as_slice().to_vec(),
                commit: commit.tls_serialize_detached().context("commit serialize")?,
                group_info: c.export_group_info_for(&group)?,
                epoch: group.epoch().as_u64(),
            })
        })
    }

    /// Adds a member — validates their KeyPackage bytes, then only
    /// **stages** the Add commit. If the server's complete accepts,
    /// merge_pending; if it rejects (stale_epoch etc.), clear_pending —
    /// locally merging a commit that was never relayed forks us from the
    /// room (§8-4: the client half of the server's atomic relay).
    /// Any pending commit left from before is cleared (crash-residue
    /// self-healing).
    pub fn add_member(&mut self, group_id: Vec<u8>, key_package: Vec<u8>) -> Result<AddOutcome> {
        self.locked(|c| {
            let kp_in = KeyPackageIn::tls_deserialize_exact(key_package.as_slice())
                .context("key package deserialize")?;
            let kp = kp_in
                .validate(c.provider.crypto(), ProtocolVersion::Mls10)
                .map_err(|e| anyhow!("key package validate: {e:?}"))?;

            let mut group = c.load_group(&group_id)?;
            // Silently clearing another operation's staging would let that
            // side's merge finalize THIS commit (Codex §8-4 P1: ownerless
            // merge). Cleanup belongs to the session-start sweep
            // (clear_pending) — here, refusing is the safe move
            if group.pending_commit().is_some() {
                bail!("unresolved pending commit");
            }

            let (commit, welcome, _group_info) = group
                .add_members(&c.provider, &c.signer, &[kp])
                .map_err(|e| anyhow!("add members: {e:?}"))?;

            Ok(AddOutcome {
                commit: commit.tls_serialize_detached().context("commit serialize")?,
                welcome: welcome
                    .tls_serialize_detached()
                    .context("welcome serialize")?,
                group_info: Vec::new(),
                epoch: group.epoch().as_u64() + 1,
            })
        })
    }

    /// Finalizes the staged commit — call only after the server accepted
    /// the relay.
    pub fn merge_pending(&mut self, group_id: Vec<u8>) -> Result<u64> {
        self.locked(|c| {
            let mut group = c.load_group(&group_id)?;
            // Idempotent: no pending = already merged (a re-run after a
            // crash between apply and journal removal) — return the
            // current epoch, not an error. Only real I/O failures may stay
            // Err, so the journal's verdict basis is preserved (§8-5-b)
            if group.pending_commit().is_none() {
                return Ok(group.epoch().as_u64());
            }
            group
                .merge_pending_commit(&c.provider)
                .map_err(|e| anyhow!("merge pending: {e:?}"))?;
            Ok(group.epoch().as_u64())
        })
    }

    /// Discards the staged commit — when the server rejected
    /// (stale_epoch·lease). Group state remains as before staging.
    /// Idempotent (no pending = success).
    pub fn clear_pending(&mut self, group_id: Vec<u8>) -> Result<()> {
        self.locked(|c| {
            let mut group = c.load_group(&group_id)?;
            if group.pending_commit().is_none() {
                return Ok(());
            }
            group
                .clear_pending_commit(c.provider.storage())
                .map_err(|e| anyhow!("clear pending: {e:?}"))?;
            Ok(())
        })
    }

    /// Joins a room from Welcome bytes — returns the joined group_id.
    pub fn join_group(&mut self, welcome: Vec<u8>) -> Result<Vec<u8>> {
        self.locked(|c| c.join_group_inner(welcome))
    }

    /// The body outside locked() — split out so agent_join_group can bind
    /// it into one transaction with the receipt (mini-review 2/2: in the
    /// window between two locked() calls a valid group without a receipt
    /// remains, and reading that absence as a mismatch destroys the group).
    pub(crate) fn join_group_inner(&mut self, welcome: Vec<u8>) -> Result<Vec<u8>> {
        {
            let c = self;
            let msg = MlsMessageIn::tls_deserialize_exact(welcome.as_slice())
                .context("welcome deserialize")?;
            // into_welcome() is test-utils-feature only — the release
            // surface is extract()
            let welcome = match msg.extract() {
                MlsMessageBodyIn::Welcome(w) => w,
                _ => bail!("not a welcome message"),
            };

            let group = StagedWelcome::new_from_welcome(
                &c.provider,
                group_config().join_config(),
                welcome,
                None,
            )
            .map_err(|e| anyhow!("staged welcome: {e:?}"))?
            .into_group(&c.provider)
            .map_err(|e| anyhow!("into group: {e:?}"))?;

            Ok(group.group_id().as_slice().to_vec())
        }
    }

    /// Encrypts an application message → the bytes carried in Envelope v2
    /// payload.ct.
    pub fn encrypt(&mut self, group_id: Vec<u8>, plaintext: Vec<u8>) -> Result<Vec<u8>> {
        self.locked(|c| {
            let mut group = c.load_group(&group_id)?;
            group
                .create_message(&c.provider, &c.signer, &plaintext)
                .map_err(|e| anyhow!("encrypt: {e:?}"))?
                .tls_serialize_detached()
                .context("message serialize")
        })
    }

    /// Processes received bytes — plaintext for application messages,
    /// state advance for commits/proposals. An echo of this device's own
    /// message is `Incoming::Own` (use the local original).
    pub fn process_incoming(&mut self, group_id: Vec<u8>, message: Vec<u8>) -> Result<Incoming> {
        self.locked(|c| c.process_incoming_inner(&group_id, &message))
    }

    /// The body outside locked() — split out so batch ingest (agent) can
    /// run multiple items inside one transaction. Never call standalone
    /// (no lock).
    pub(crate) fn process_incoming_inner(
        &mut self,
        group_id: &[u8],
        message: &[u8],
    ) -> Result<Incoming> {
        {
            let c = self;
            let msg = MlsMessageIn::tls_deserialize_exact(message)
                .context("message deserialize")?;
            let protocol_message: ProtocolMessage = msg
                .try_into_protocol_message()
                .map_err(|e| anyhow!("not a protocol message: {e:?}"))?;

            let mut group = c.load_group(group_id)?;

            let msg_epoch = protocol_message.epoch();
            let processed = match group.process_message(&c.provider, protocol_message) {
                Ok(processed) => processed,
                Err(ProcessMessageError::ValidationError(
                    ValidationError::CannotDecryptOwnMessage,
                )) => return Ok(Incoming::own()),
                // A handshake echo from a past epoch — canonical case: a
                // commit I created and already merged coming back on the
                // relay (the real-device bug of 2026-07-19 where the
                // room's first message rendered as "message unavailable"
                // for the approver). Our state is already past it —
                // report it in the same harmless class as an own echo.
                // A future epoch is different: WE are behind, so leave it
                // as an error
                Err(ProcessMessageError::ValidationError(ValidationError::WrongEpoch))
                    if msg_epoch < group.epoch() =>
                {
                    return Ok(Incoming::own())
                }
                Err(e) => return Err(anyhow!("process message: {e:?}")),
            };

            let sender = basic_identity(processed.credential());

            match processed.into_content() {
                ProcessedMessageContent::ApplicationMessage(app) => {
                    Ok(Incoming::application(sender, app.into_bytes()))
                }

                ProcessedMessageContent::StagedCommitMessage(staged) => {
                    // §8-4 gate: external join commits are rejected without
                    // merging. Without a merge the group state is untouched
                    // (a commit is public handshake — it consumes no secret
                    // ratchet)
                    if c.reject_external_init && has_external_init(&staged) {
                        return Ok(Incoming::rejected_external_join());
                    }

                    // Member diff before/after the commit (keyed by
                    // signature key) — catches Add·Remove·external joins
                    // alike. Under the §2 rule (no key rotation) a
                    // signature-key change also surfaces as remove+add =
                    // exactly the "impossible event" the AS wants
                    let before: Vec<MemberInfo> = group.members().map(member_info).collect();

                    group
                        .merge_staged_commit(&c.provider, *staged)
                        .map_err(|e| anyhow!("merge staged commit: {e:?}"))?;

                    let after: Vec<MemberInfo> = group.members().map(member_info).collect();
                    let added = after
                        .iter()
                        .filter(|a| !before.iter().any(|b| b.signature_key == a.signature_key))
                        .cloned()
                        .collect();
                    let removed = before
                        .into_iter()
                        .filter(|b| !after.iter().any(|a| a.signature_key == b.signature_key))
                        .collect();

                    Ok(Incoming::handshake(group.epoch().as_u64(), added, removed))
                }

                ProcessedMessageContent::ProposalMessage(proposal)
                | ProcessedMessageContent::ExternalJoinProposalMessage(proposal) => {
                    group
                        .store_pending_proposal(c.provider.storage(), *proposal)
                        .map_err(|e| anyhow!("store proposal: {e:?}"))?;

                    Ok(Incoming::handshake(
                        group.epoch().as_u64(),
                        Vec::new(),
                        Vec::new(),
                    ))
                }
            }
        }
    }

    /// The external join (ExternalInit) commit rejection policy — the §8-4
    /// cutover gate. Enabled after the Welcome-only cutover: the server
    /// never parses MLS bytes, so the final defense against forged or
    /// residual external commits is the client (spec §3-5).
    pub fn set_reject_external_init(&mut self, enabled: bool) {
        self.reject_external_init = enabled;
    }

    /// The current member (leaf) list — material for the approving
    /// device's pre-Add stale-leaf check (spec §4) and the full AS
    /// registry comparison. Two leaves with the same identity means a
    /// ghost.
    pub fn list_members(&mut self, group_id: Vec<u8>) -> Result<Vec<MemberInfo>> {
        self.locked(|c| {
            let group = c.load_group(&group_id)?;
            Ok(group.members().map(member_info).collect())
        })
    }

    /// Removes a member — only **stages** a Remove commit deleting **all**
    /// leaves of the same identity (device) (merge_pending after relay
    /// acceptance, clear_pending on rejection — the same two-phase as
    /// add_member). Leaving, kicking, orphan cleanup (spec §3-7), and
    /// stale-leaf cleanup (§4) all take this path. You cannot remove
    /// yourself (my departure is deleted by a remaining device).
    pub fn remove_member(&mut self, group_id: Vec<u8>, identity: String) -> Result<RemoveOutcome> {
        self.locked(|c| {
            let mut group = c.load_group(&group_id)?;
            // Same reason as add_member — never stand on someone else's
            // staging
            if group.pending_commit().is_some() {
                bail!("unresolved pending commit");
            }

            let targets: Vec<LeafNodeIndex> = group
                .members()
                .filter(|m| basic_identity(&m.credential) == identity)
                .map(|m| m.index)
                .collect();
            if targets.is_empty() {
                bail!("member not found: {identity}");
            }

            // Commit only the requested Remove — remove_members()'s
            // default also consumes the pending proposal store, letting
            // queued third-party Add/Removes ride this commit unnoticed
            // (Codex §8-3 P1: a discarded Welcome = a ghost leaf)
            let commit = group
                .commit_builder()
                .consume_proposal_store(false)
                .propose_removals(targets)
                .load_psks(c.provider.storage())
                .map_err(|e| anyhow!("remove members: {e:?}"))?
                .build(c.provider.rand(), c.provider.crypto(), &c.signer, |_| true)
                .map_err(|e| anyhow!("remove members: {e:?}"))?
                .stage_commit(&c.provider)
                .map_err(|e| anyhow!("remove members: {e:?}"))?
                .into_commit();

            Ok(RemoveOutcome {
                commit: commit.tls_serialize_detached().context("commit serialize")?,
                group_info: Vec::new(),
                // Staged state — the epoch after this commit applies
                // (actual after merge_pending)
                epoch: group.epoch().as_u64() + 1,
            })
        })
    }

    /// Discards the local group — when a join was closed just before
    /// confirmation (JoinClosed, request expired), deleting the group made
    /// from the Welcome keeps a re-join of the same room (a new Welcome)
    /// unblocked. Idempotent — a missing group is ignored.
    pub fn delete_group(&mut self, group_id: Vec<u8>) -> Result<()> {
        self.locked(|c| {
            // Receipts hold decrypted plaintext — folding the room folds
            // them too, and the cursor must not outlive the group
            // (impl /133 P2)
            #[cfg(feature = "agent-cli")]
            {
                let mut p = Vec::new();
                p.extend_from_slice(AGENT_NS);
                p.extend_from_slice(&(group_id.len() as u32).to_be_bytes());
                p.extend_from_slice(&group_id);
                if let Ok(mut m) = c.provider.storage().values.write() {
                    m.retain(|k, _| !k.starts_with(&p));
                }
            }
            if let Some(mut group) =
                MlsGroup::load(c.provider.storage(), &GroupId::from_slice(&group_id))
                    .map_err(|e| anyhow!("load group: {e:?}"))?
            {
                group
                    .delete(c.provider.storage())
                    .map_err(|e| anyhow!("delete group: {e:?}"))?;
            }
            Ok(())
        })
    }

    /// Whether this device holds state for this group (used to judge a
    /// room's MLS transition).
    pub fn has_group(&mut self, group_id: Vec<u8>) -> Result<bool> {
        self.locked(|c| {
            Ok(
                MlsGroup::load(c.provider.storage(), &GroupId::from_slice(&group_id))
                    .map_err(|e| anyhow!("load group: {e:?}"))?
                    .is_some(),
            )
        })
    }

    /// The current epoch — a GroupInfo PUT parameter.
    pub fn group_epoch(&mut self, group_id: Vec<u8>) -> Result<u64> {
        self.locked(|c| Ok(c.load_group(&group_id)?.epoch().as_u64()))
    }

    // ── Internals ─────────────────────────────────────────────────────

    /// The common path of every public operation: flock → file reload →
    /// operate → save. We always start on top of whatever state another
    /// process (the NSE) advanced in between.
    fn locked<T>(&mut self, f: impl FnOnce(&mut Self) -> Result<T>) -> Result<T> {
        let _lock = FileLock::exclusive(&self.dir.join(LOCK_FILE))?;
        self.reload()?;
        let result = f(self);
        if result.is_ok() {
            self.save()?;
        }
        result
    }

    fn reload(&mut self) -> Result<()> {
        let state_path = self.dir.join(STATE_FILE);
        if !state_path.exists() {
            return Ok(());
        }

        let state = StateFile::read(&state_path)?;
        if state.identity != self.identity {
            bail!("identity mismatch on reload");
        }
        // The policy flag lives outside the state file — a reload must not
        // erase it
        let reject_external_init = self.reject_external_init;
        *self = Self::from_state(self.dir.clone(), state)?;
        self.reject_external_init = reject_external_init;
        Ok(())
    }

    fn save(&self) -> Result<()> {
        let values = self
            .provider
            .storage()
            .values
            .read()
            .map_err(|_| anyhow!("storage lock poisoned"))?
            .clone();

        let state = StateFile {
            identity: self.identity.clone(),
            // The keypair goes whole via tls_codec — the private accessor
            // is test-utils only
            signer: self
                .signer
                .tls_serialize_detached()
                .map_err(|e| anyhow!("signer serialize: {e:?}"))?,
            values,
        };
        state.write(&self.dir.join(STATE_FILE))
    }

    fn from_state(dir: PathBuf, state: StateFile) -> Result<MlsClient> {
        let provider = OpenMlsRustCrypto::default();
        *provider.storage().values.write().unwrap() = state.values;

        let signer = SignatureKeyPair::tls_deserialize_exact(state.signer.as_slice())
            .map_err(|e| anyhow!("signer deserialize: {e:?}"))?;
        let credential = BasicCredential::new(state.identity.clone().into_bytes());

        Ok(MlsClient {
            dir,
            identity: state.identity,
            credential_with_key: CredentialWithKey {
                credential: credential.into(),
                signature_key: signer.to_public_vec().into(),
            },
            provider,
            signer,
            reject_external_init: REJECT_EXTERNAL_INIT_DEFAULT,
        })
    }

    fn load_group(&self, group_id: &[u8]) -> Result<MlsGroup> {
        MlsGroup::load(self.provider.storage(), &GroupId::from_slice(group_id))
            .map_err(|e| anyhow!("load group: {e:?}"))?
            .ok_or_else(|| anyhow!("unknown group"))
    }

    fn export_group_info_for(&self, group: &MlsGroup) -> Result<Vec<u8>> {
        group
            .export_group_info(self.provider.crypto(), &self.signer, true)
            .map_err(|e| anyhow!("export group info: {e:?}"))?
            .tls_serialize_detached()
            .context("group info serialize")
    }
}

/// Room config — the ratchet tree rides in Welcome/GroupInfo so the server
/// never needs to know the tree.
fn group_config() -> MlsGroupCreateConfig {
    MlsGroupCreateConfig::builder()
        .ciphersuite(CIPHERSUITE)
        .use_ratchet_tree_extension(true)
        .build()
}

fn basic_identity(credential: &Credential) -> String {
    BasicCredential::try_from(credential.clone())
        .map(|c| String::from_utf8_lossy(c.identity()).into_owned())
        .unwrap_or_default()
}

fn member_info(member: Member) -> MemberInfo {
    MemberInfo {
        leaf_index: member.index.u32(),
        identity: basic_identity(&member.credential),
        signature_key: member.signature_key,
    }
}

/// Whether the commit carries an ExternalInit proposal — the marker of an
/// external join commit.
fn has_external_init(staged: &StagedCommit) -> bool {
    staged
        .queued_proposals()
        .any(|p| matches!(p.proposal(), Proposal::ExternalInit(_)))
}

// ── State file ─────────────────────────────────────────────────────────
//
// identity + signing keypair + the entire OpenMLS storage map in a single
// file. Length-prefixed binary (no external dependency); writes are atomic
// via tmp → rename.

struct StateFile {
    identity: String,
    /// The SignatureKeyPair serialized via tls_codec
    signer: Vec<u8>,
    values: HashMap<Vec<u8>, Vec<u8>>,
}

impl StateFile {
    fn read(path: &Path) -> Result<StateFile> {
        let mut bytes = Vec::new();
        File::open(path)
            .context("open state file")?
            .read_to_end(&mut bytes)
            .context("read state file")?;

        let mut cursor = 0usize;
        let magic = take(&bytes, &mut cursor, STATE_MAGIC.len())?;
        if magic != STATE_MAGIC {
            bail!("bad state file magic");
        }

        let identity =
            String::from_utf8(take_chunk(&bytes, &mut cursor)?).context("state identity utf8")?;
        let signer = take_chunk(&bytes, &mut cursor)?;

        let entry_count = take_u32(&bytes, &mut cursor)? as usize;
        let mut values = HashMap::with_capacity(entry_count);
        for _ in 0..entry_count {
            let key = take_chunk(&bytes, &mut cursor)?;
            let value = take_chunk(&bytes, &mut cursor)?;
            values.insert(key, value);
        }

        Ok(StateFile {
            identity,
            signer,
            values,
        })
    }

    fn write(&self, path: &Path) -> Result<()> {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(STATE_MAGIC);
        put_chunk(&mut bytes, self.identity.as_bytes());
        put_chunk(&mut bytes, &self.signer);
        put_u32(&mut bytes, self.values.len() as u32);
        for (key, value) in &self.values {
            put_chunk(&mut bytes, key);
            put_chunk(&mut bytes, value);
        }

        let tmp = path.with_extension("tmp");
        {
            let mut file = File::create(&tmp).context("create tmp state")?;
            file.write_all(&bytes).context("write state")?;
            file.sync_all().context("sync state")?;
        }
        fs::rename(&tmp, path).context("swap state file")?;
        Ok(())
    }
}

fn put_u32(out: &mut Vec<u8>, n: u32) {
    out.extend_from_slice(&n.to_le_bytes());
}

fn put_chunk(out: &mut Vec<u8>, chunk: &[u8]) {
    put_u32(out, chunk.len() as u32);
    out.extend_from_slice(chunk);
}

fn take<'a>(bytes: &'a [u8], cursor: &mut usize, len: usize) -> Result<&'a [u8]> {
    let end = cursor
        .checked_add(len)
        .filter(|&end| end <= bytes.len())
        .ok_or_else(|| anyhow!("truncated state file"))?;
    let slice = &bytes[*cursor..end];
    *cursor = end;
    Ok(slice)
}

fn take_u32(bytes: &[u8], cursor: &mut usize) -> Result<u32> {
    let raw = take(bytes, cursor, 4)?;
    Ok(u32::from_le_bytes(raw.try_into().unwrap()))
}

fn take_chunk(bytes: &[u8], cursor: &mut usize) -> Result<Vec<u8>> {
    let len = take_u32(bytes, cursor)? as usize;
    Ok(take(bytes, cursor, len)?.to_vec())
}

// ── Inter-process lock ─────────────────────────────────────────────────

struct FileLock(File);

impl FileLock {
    fn exclusive(path: &Path) -> Result<FileLock> {
        let file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(false)
            .open(path)
            .context("open lock file")?;

        // unix (Android included) uses libc flock(LOCK_EX): std File::lock
        // is unimplemented on the android target and dies with Unsupported
        // — the measured regression that silently killed all of Android
        // E2EE in the 46 aab (2026-08-14, emulator). Windows alone uses
        // std File::lock (LockFileEx) — libc is a unix-target dependency,
        // which also keeps the Windows compile intact (2026-08-14 CI E0433).
        #[cfg(unix)]
        {
            let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) };
            if rc != 0 {
                bail!("flock failed: {}", std::io::Error::last_os_error());
            }
        }
        #[cfg(not(unix))]
        file.lock().context("lock file")?;
        Ok(FileLock(file))
    }
}

impl Drop for FileLock {
    fn drop(&mut self) {
        #[cfg(unix)]
        unsafe {
            libc::flock(self.0.as_raw_fd(), libc::LOCK_UN);
        }
        #[cfg(not(unix))]
        {
            let _ = self.0.unlock();
        }
    }
}

// ── Agent helper extension (agent-mls-v1 §1, feature "agent-cli" only) ──
//
// The Python CLI's receive path. The cursor and receipts live not in a
// separate file but in this state file's values map (KAGENT-namespaced
// keys), so the MLS ratchet advance and "processed through seq N, and
// here is the result" commit together in the same tmp→rename — a process
// kill can produce neither reprocessing nor plaintext loss (/133 verdict
// ⑤). A receipt survives until Python fsyncs it into the local ledger and
// clears it with ingest-ack, protecting the plaintext through the window
// where "the helper succeeded but died before the ledger write".

#[cfg(feature = "agent-cli")]
pub struct IngestOutcome {
    pub seq: u64,
    /// A re-call of an already-processed seq — the stored receipt was
    /// returned as-is
    pub replayed: bool,
    /// replayed but the receipt was already cleared by ack — meaning it is
    /// already in the ledger
    pub pruned: bool,
    pub incoming: Option<Incoming>,
    /// Processing failure — ingest stopped at this seq and the cursor did
    /// not advance
    pub error: Option<String>,
}

#[cfg(feature = "agent-cli")]
const AGENT_NS: &[u8] = b"KAGENT\x00";
#[cfg(feature = "agent-cli")]
const AGENT_CURSOR: u8 = 0x01;
#[cfg(feature = "agent-cli")]
const AGENT_RECEIPT: u8 = 0x02;
#[cfg(feature = "agent-cli")]
const AGENT_JOINREQ: u8 = 0x03;

#[cfg(feature = "agent-cli")]
impl MlsClient {
    fn agent_key(group_id: &[u8], kind: u8, seq: u64) -> Vec<u8> {
        let mut k = Vec::with_capacity(AGENT_NS.len() + 4 + group_id.len() + 9);
        k.extend_from_slice(AGENT_NS);
        k.extend_from_slice(&(group_id.len() as u32).to_be_bytes());
        k.extend_from_slice(group_id);
        k.push(kind);
        if kind == AGENT_RECEIPT {
            k.extend_from_slice(&seq.to_be_bytes());
        }
        k
    }

    fn values_get(&self, key: &[u8]) -> Option<Vec<u8>> {
        self.provider.storage().values.read().ok()?.get(key).cloned()
    }

    fn values_put(&self, key: Vec<u8>, value: Vec<u8>) {
        if let Ok(mut m) = self.provider.storage().values.write() {
            m.insert(key, value);
        }
    }

    /// This group's built-in cursor — the last definitively processed seq
    /// (None if absent). The accompanying resume is **the resume point**:
    /// if surviving (un-acked) receipts exist, it is their minimum seq−1 —
    /// plaintext from the window of dying before the ledger fsync is
    /// recoverable only by receipt replay, so callers must page from
    /// resume, not from the cursor (impl /133 P0: paging from the cursor
    /// makes receipts dead code forever).
    pub fn agent_cursor(&mut self, group_id: Vec<u8>) -> Result<(Option<u64>, Option<u64>)> {
        self.locked(|c| {
            let cursor = c
                .values_get(&Self::agent_key(&group_id, AGENT_CURSOR, 0))
                .and_then(|v| v.try_into().ok().map(u64::from_be_bytes));
            let prefix = Self::agent_receipt_prefix(&group_id);
            let min_receipt = c
                .provider
                .storage()
                .values
                .read()
                .ok()
                .and_then(|m| {
                    m.keys()
                        .filter(|k| k.starts_with(&prefix) && k.len() == prefix.len() + 8)
                        .filter_map(|k| {
                            k[prefix.len()..].try_into().ok().map(u64::from_be_bytes)
                        })
                        .min()
                });
            let resume = match (cursor, min_receipt) {
                (Some(c), Some(r)) => Some(c.min(r.saturating_sub(1))),
                (c, _) => c,
            };
            Ok((cursor, resume))
        })
    }

    fn agent_receipt_prefix(group_id: &[u8]) -> Vec<u8> {
        let mut p = Vec::new();
        p.extend_from_slice(AGENT_NS);
        p.extend_from_slice(&(group_id.len() as u32).to_be_bytes());
        p.extend_from_slice(group_id);
        p.push(AGENT_RECEIPT);
        p
    }

    /// Skips one undecryptable item as a placeholder — cursor advances
    /// only, ratchet untouched. Never used for commit (handshake) failures
    /// (design §4-3).
    pub fn agent_ingest_skip(&mut self, group_id: Vec<u8>, seq: u64) -> Result<()> {
        self.locked(|c| {
            let key = Self::agent_key(&group_id, AGENT_CURSOR, 0);
            let cur = c
                .values_get(&key)
                .and_then(|v| v.try_into().ok().map(u64::from_be_bytes));
            if !cur.is_some_and(|p| seq <= p) {
                c.values_put(key, seq.to_be_bytes().to_vec());
            }
            Ok(())
        })
    }

    /// The join receipt — records the request_id in the same transaction
    /// as join-group, preventing a stale group from being mistaken for a
    /// new Welcome (design §3-5).
    pub fn agent_join_group(
        &mut self,
        welcome: Vec<u8>,
        request_id: String,
    ) -> Result<Vec<u8>> {
        // The group and the receipt commit under the same flock, the same
        // save (mini-review 2/2)
        self.locked(|c| {
            let group_id = c.join_group_inner(welcome)?;
            c.values_put(
                Self::agent_key(&group_id, AGENT_JOINREQ, 0),
                request_id.into_bytes(),
            );
            Ok(group_id)
        })
    }

    pub fn agent_join_receipt(&mut self, group_id: Vec<u8>) -> Result<Option<String>> {
        self.locked(|c| {
            Ok(c
                .values_get(&Self::agent_key(&group_id, AGENT_JOINREQ, 0))
                .map(|v| String::from_utf8_lossy(&v).into_owned()))
        })
    }

    /// epoch-CAS encryption — locks in only when the epoch matches the one
    /// at roster-verification time. If another process digested an Add in
    /// between, reject with roster_changed (impl /133 P0: blocks secret
    /// distribution to unverified leaves in the verify↔encrypt window).
    pub fn agent_encrypt(
        &mut self,
        group_id: Vec<u8>,
        expected_epoch: u64,
        plaintext: Vec<u8>,
    ) -> Result<Vec<u8>> {
        self.locked(|c| {
            let mut group = c.load_group(&group_id)?;
            if group.epoch().as_u64() != expected_epoch {
                bail!("roster_changed");
            }
            group
                .create_message(&c.provider, &c.signer, &plaintext)
                .map_err(|e| anyhow!("encrypt: {e:?}"))?
                .tls_serialize_detached()
                .context("message serialize")
        })
    }

    /// Batch receive — commits (decrypt + cursor advance + receipt store)
    /// together inside one flock, one save. items must be ascending by
    /// seq, and processing stops at a failed seq (that seq's cursor is not
    /// finalized — an ingress stop leads to the caller's (Python's) desync
    /// disposition, design §4-3).
    pub fn agent_ingest(
        &mut self,
        group_id: Vec<u8>,
        items: Vec<(u64, Vec<u8>)>,
    ) -> Result<Vec<IngestOutcome>> {
        self.locked(|c| {
            let cursor_key = Self::agent_key(&group_id, AGENT_CURSOR, 0);
            let mut cursor: Option<u64> = c
                .values_get(&cursor_key)
                .and_then(|v| v.try_into().ok().map(u64::from_be_bytes));

            let mut out = Vec::with_capacity(items.len());
            let mut last_in_batch: Option<u64> = None;

            for (seq, ct) in items {
                if last_in_batch.is_some_and(|p| seq <= p) {
                    out.push(IngestOutcome {
                        seq,
                        replayed: false,
                        pruned: false,
                        incoming: None,
                        error: Some("batch not ascending".into()),
                    });
                    break;
                }
                last_in_batch = Some(seq);

                if cursor.is_some_and(|cur| seq <= cur) {
                    // A re-call — return the stored result without
                    // consuming the ratchet again
                    let receipt_key = Self::agent_key(&group_id, AGENT_RECEIPT, seq);
                    match c.values_get(&receipt_key) {
                        Some(bytes) => out.push(IngestOutcome {
                            seq,
                            replayed: true,
                            pruned: false,
                            incoming: Some(receipt_decode(&bytes)?),
                            error: None,
                        }),
                        None => out.push(IngestOutcome {
                            seq,
                            replayed: true,
                            pruned: true,
                            incoming: None,
                            error: None,
                        }),
                    }
                    continue;
                }

                // Snapshot at item start — if process succeeds but merge
                // fails, a half-state with only the secrets consumed would
                // get saved (impl /133 P2)
                let snapshot = c
                    .provider
                    .storage()
                    .values
                    .read()
                    .map_err(|_| anyhow!("storage lock poisoned"))?
                    .clone();
                match c.process_incoming_inner(&group_id, &ct) {
                    Ok(incoming) => {
                        let own_key = c.signer.public().to_vec();
                        let self_removed = incoming.removed.iter().any(|m| {
                            // identity alone is not enough — it would
                            // mistake the removal of a ghost leaf using
                            // the same device_id for our own kick
                            m.identity == c.identity && m.signature_key == own_key
                        });
                        c.values_put(
                            Self::agent_key(&group_id, AGENT_RECEIPT, seq),
                            receipt_encode(&incoming),
                        );
                        cursor = Some(seq);
                        c.values_put(cursor_key.clone(), seq.to_be_bytes().to_vec());
                        out.push(IngestOutcome {
                            seq,
                            replayed: false,
                            pruned: false,
                            incoming: Some(incoming),
                            error: None,
                        });
                        // Rows after our own Remove are undecryptable
                        // anyway — stop here; disposition belongs to the
                        // caller (§5-4) (impl /133 4/6)
                        if self_removed {
                            break;
                        }
                    }
                    Err(e) => {
                        if let Ok(mut m) = c.provider.storage().values.write() {
                            *m = snapshot;
                        }
                        out.push(IngestOutcome {
                            seq,
                            replayed: false,
                            pruned: false,
                            incoming: None,
                            error: Some(format!("{e:#}")),
                        });
                        break;
                    }
                }
            }
            Ok(out)
        })
    }

    /// Receipt cleanup after ledger finalization — called only after
    /// Python fsynced the plaintext. The cursor stays (the cursor is the
    /// canon against reprocessing; receipts are the loss-prevention
    /// window).
    pub fn agent_ingest_ack(&mut self, group_id: Vec<u8>, upto_seq: u64) -> Result<u64> {
        self.locked(|c| {
            let prefix = {
                let mut p = Vec::new();
                p.extend_from_slice(AGENT_NS);
                p.extend_from_slice(&(group_id.len() as u32).to_be_bytes());
                p.extend_from_slice(&group_id);
                p.push(AGENT_RECEIPT);
                p
            };
            let mut removed = 0u64;
            if let Ok(mut m) = c.provider.storage().values.write() {
                let doomed: Vec<Vec<u8>> = m
                    .keys()
                    .filter(|k| {
                        k.starts_with(&prefix)
                            && k.len() == prefix.len() + 8
                            && k[prefix.len()..]
                                .try_into()
                                .ok()
                                .map(u64::from_be_bytes)
                                .is_some_and(|s| s <= upto_seq)
                    })
                    .cloned()
                    .collect();
                for k in doomed {
                    m.remove(&k);
                    removed += 1;
                }
            }
            Ok(removed)
        })
    }
}

/// Receipt serialization — a processing result living inside the state
/// file. Length-prefixed binary (same grammar as the state file, no
/// external dependency).
#[cfg(feature = "agent-cli")]
fn receipt_encode(inc: &Incoming) -> Vec<u8> {
    let mut b = Vec::new();
    b.push(match inc.kind {
        IncomingKind::Application => 0,
        IncomingKind::Handshake => 1,
        IncomingKind::Own => 2,
        IncomingKind::RejectedExternalJoin => 3,
    });
    put_chunk(&mut b, inc.sender.as_deref().unwrap_or("").as_bytes());
    put_chunk(&mut b, inc.plaintext.as_deref().unwrap_or(&[]));
    b.extend_from_slice(&inc.epoch.map(|e| e + 1).unwrap_or(0).to_be_bytes());
    for list in [&inc.added, &inc.removed] {
        put_u32(&mut b, list.len() as u32);
        for m in list.iter() {
            b.extend_from_slice(&m.leaf_index.to_be_bytes());
            put_chunk(&mut b, m.identity.as_bytes());
            put_chunk(&mut b, &m.signature_key);
        }
    }
    b
}

#[cfg(feature = "agent-cli")]
fn receipt_decode(bytes: &[u8]) -> Result<Incoming> {
    let mut cur = 0usize;
    let kind_byte = take(bytes, &mut cur, 1)?[0];
    let kind = match kind_byte {
        0 => IncomingKind::Application,
        1 => IncomingKind::Handshake,
        2 => IncomingKind::Own,
        3 => IncomingKind::RejectedExternalJoin,
        _ => bail!("bad receipt kind"),
    };
    let sender = String::from_utf8(take_chunk(bytes, &mut cur)?).context("receipt sender")?;
    let plaintext = take_chunk(bytes, &mut cur)?;
    let epoch_raw = u64::from_be_bytes(take(bytes, &mut cur, 8)?.try_into().unwrap());
    let mut lists: [Vec<MemberInfo>; 2] = [Vec::new(), Vec::new()];
    for list in lists.iter_mut() {
        let n = take_u32(bytes, &mut cur)? as usize;
        for _ in 0..n {
            let leaf_index = u32::from_be_bytes(take(bytes, &mut cur, 4)?.try_into().unwrap());
            let identity =
                String::from_utf8(take_chunk(bytes, &mut cur)?).context("receipt identity")?;
            let signature_key = take_chunk(bytes, &mut cur)?;
            list.push(MemberInfo {
                leaf_index,
                identity,
                signature_key,
            });
        }
    }
    let [added, removed] = lists;
    Ok(Incoming {
        kind,
        sender: if sender.is_empty() { None } else { Some(sender) },
        plaintext: if plaintext.is_empty() {
            None
        } else {
            Some(plaintext)
        },
        epoch: if epoch_raw == 0 {
            None
        } else {
            Some(epoch_raw - 1)
        },
        added,
        removed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_dir(name: &str) -> String {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir()
            .join(format!("klatalk_mls_test_{name}_{nanos}"))
            .to_string_lossy()
            .into_owned()
    }

    fn client(name: &str) -> (MlsClient, String) {
        let dir = test_dir(name);
        (
            MlsClient::open(dir.clone(), format!("{name}-device")).unwrap(),
            dir,
        )
    }

    // add/remove are two-phase (stage→merge) — the tests' "the server
    // accepted" shorthand
    fn add_merged(c: &mut MlsClient, room: &[u8], kp: Vec<u8>) -> AddOutcome {
        let out = c.add_member(room.to_vec(), kp).unwrap();
        c.merge_pending(room.to_vec()).unwrap();
        out
    }

    fn remove_merged(c: &mut MlsClient, room: &[u8], identity: &str) -> RemoveOutcome {
        let out = c.remove_member(room.to_vec(), identity.to_string()).unwrap();
        c.merge_pending(room.to_vec()).unwrap();
        out
    }

    #[test]
    fn add_welcome_round_trip() {
        let (mut a, _) = client("alice");
        let (mut b, _) = client("bob");

        let room_id = b"room-uuid-bytes".to_vec();
        a.create_group(room_id.clone()).unwrap();

        let b_kp = b.create_key_package().unwrap();
        let outcome = add_merged(&mut a, &room_id, b_kp);
        assert!(outcome.epoch >= 1);
        let joined = b.join_group(outcome.welcome).unwrap();
        assert_eq!(joined, room_id);

        let plaintext = "엄마, 이제 여기서 얘기해요".as_bytes().to_vec();
        let ct = a.encrypt(room_id.clone(), plaintext.clone()).unwrap();
        assert_ne!(ct, plaintext);

        let incoming = b.process_incoming(room_id.clone(), ct).unwrap();
        assert!(matches!(incoming.kind, IncomingKind::Application));
        assert_eq!(incoming.plaintext.unwrap(), plaintext);
        assert_eq!(incoming.sender.unwrap(), "alice-device");

        let ct2 = b.encrypt(room_id.clone(), b"reply".to_vec()).unwrap();
        let reply = a.process_incoming(room_id, ct2).unwrap();
        assert_eq!(reply.plaintext.unwrap(), b"reply");
    }

    #[test]
    fn sealed_signer_survives_state_loss() {
        // The reinstall scenario: state file gone, only the keychain seal
        // survives (rekey §7)
        let (mut a, dir) = client("sealed");
        let sealed = a.export_signing_keypair().unwrap();
        let pk_before = a.signing_public_key().unwrap();
        drop(a);
        std::fs::remove_dir_all(&dir).unwrap();

        let mut b = MlsClient::open_sealed(dir, "sealed-device".into(), sealed).unwrap();
        assert_eq!(b.signing_public_key().unwrap(), pk_before);

        // Does the restored private key actually sign? — a KeyPackage
        // create-and-validate round trip
        let kp = b.create_key_package().unwrap();
        let info = b.key_package_info(kp).unwrap();
        assert_eq!(info.signature_key, pk_before);
        assert_eq!(info.identity, "sealed-device");
    }

    #[test]
    fn open_sealed_prefers_existing_state() {
        // With a live state file, the sealed argument (a different key) is
        // ignored — the state file is canon
        let (mut a, dir) = client("sealed-live");
        let pk = a.signing_public_key().unwrap();
        drop(a);

        let (mut other, _) = client("sealed-other");
        let foreign = other.export_signing_keypair().unwrap();

        let mut b = MlsClient::open_sealed(dir, "sealed-live-device".into(), foreign).unwrap();
        assert_eq!(b.signing_public_key().unwrap(), pk);
    }

    #[test]
    fn open_sealed_rejects_garbage() {
        let dir = test_dir("sealed-bad");
        assert!(MlsClient::open_sealed(dir, "d".into(), vec![1, 2, 3]).is_err());
    }

    #[test]
    fn sign_verifies_with_signing_public_key() {
        // Rekey §3 corollary 2: signatures verify against the registry's
        // current key (= signing_public_key). Must be raw Ed25519 64 bytes,
        // same as the server (Erlang :crypto.verify eddsa).
        use ed25519_dalek::{Signature, Verifier, VerifyingKey};

        let (mut c, _) = client("signer");
        let pk = c.signing_public_key().unwrap();
        let message = b"klatalk-rekey-approve-v1-payload".to_vec();
        let sig = c.sign(message.clone()).unwrap();

        assert_eq!(sig.len(), 64);
        let vk = VerifyingKey::from_bytes(pk.as_slice().try_into().unwrap()).unwrap();
        let signature = Signature::from_slice(&sig).unwrap();
        assert!(vk.verify(&message, &signature).is_ok());
    }

    #[test]
    fn external_commit_join() {
        let (mut a, _) = client("host");
        let (mut c, _) = client("newcomer");
        // State the gate policy explicitly — deterministic regardless of
        // the compile-time default (§8-4 build)
        a.set_reject_external_init(false);

        let room_id = b"external-room".to_vec();
        let group_info = a.create_group(room_id.clone()).unwrap();

        // Join on our own using the GroupInfo received from the server
        let joined = c.join_by_external_commit(group_info).unwrap();
        assert_eq!(joined.group_id, room_id);
        assert_eq!(joined.epoch, 1);

        // The joiner's commit is relayed to existing members and advances
        // their state
        let handshake = a.process_incoming(room_id.clone(), joined.commit).unwrap();
        assert!(matches!(handshake.kind, IncomingKind::Handshake));
        assert_eq!(handshake.epoch.unwrap(), 1);

        // Now a bidirectional encrypted round trip
        let ct = c
            .encrypt(room_id.clone(), b"knock knock".to_vec())
            .unwrap();
        let knock = a.process_incoming(room_id.clone(), ct).unwrap();
        assert_eq!(knock.plaintext.unwrap(), b"knock knock");
        assert_eq!(knock.sender.unwrap(), "newcomer-device");

        let ct2 = a
            .encrypt(room_id.clone(), b"who's there".to_vec())
            .unwrap();
        let who = c.process_incoming(room_id, ct2).unwrap();
        assert_eq!(who.plaintext.unwrap(), b"who's there");
    }

    #[test]
    fn persistence_survives_reopen() {
        let (mut a, a_dir) = client("keeper");
        let (mut b, _) = client("partner");

        let room_id = b"persistent-room".to_vec();
        a.create_group(room_id.clone()).unwrap();
        let b_kp = b.create_key_package().unwrap();
        let outcome = add_merged(&mut a, &room_id, b_kp);
        b.join_group(outcome.welcome).unwrap();

        let ct1 = a
            .encrypt(room_id.clone(), b"before restart".to_vec())
            .unwrap();

        // Simulate a process restart — reopen from the same directory
        drop(a);
        let mut a = MlsClient::open(a_dir, "keeper-device".to_string()).unwrap();
        assert!(a.has_group(room_id.clone()).unwrap());

        let ct2 = a
            .encrypt(room_id.clone(), b"after restart".to_vec())
            .unwrap();

        for (ct, expected) in [
            (ct1, b"before restart".as_slice()),
            (ct2, b"after restart"),
        ] {
            let incoming = b.process_incoming(room_id.clone(), ct).unwrap();
            assert_eq!(incoming.plaintext.unwrap(), expected);
        }
    }

    #[test]
    fn own_message_echo_is_flagged() {
        let (mut a, _) = client("echo");
        let room_id = b"echo-room".to_vec();
        a.create_group(room_id.clone()).unwrap();

        let ct = a
            .encrypt(room_id.clone(), b"talking to myself".to_vec())
            .unwrap();
        let incoming = a.process_incoming(room_id, ct).unwrap();
        assert!(matches!(incoming.kind, IncomingKind::Own));
    }

    #[test]
    fn identity_mismatch_rejected() {
        let dir = test_dir("mismatch");
        let _a = MlsClient::open(dir.clone(), "device-one".to_string()).unwrap();
        assert!(MlsClient::open(dir, "device-two".to_string()).is_err());
    }

    // ── Phase A §8-3 additive ──────────────────────────────────────────

    #[test]
    fn list_members_and_added_diff() {
        let (mut a, _) = client("alice2");
        let (mut b, _) = client("bob2");
        let (mut c, _) = client("carol2");

        let room_id = b"members-room".to_vec();
        a.create_group(room_id.clone()).unwrap();

        let outcome = add_merged(&mut a, &room_id, b.create_key_package().unwrap());
        b.join_group(outcome.welcome).unwrap();

        // The member list as B sees it — two, identities are devices
        let members = b.list_members(room_id.clone()).unwrap();
        let ids: Vec<&str> = members.iter().map(|m| m.identity.as_str()).collect();
        assert_eq!(members.len(), 2);
        assert!(ids.contains(&"alice2-device") && ids.contains(&"bob2-device"));
        assert!(members.iter().all(|m| !m.signature_key.is_empty()));

        // A adds C — B sees the added leaf in the commit diff (AS material)
        let outcome = add_merged(&mut a, &room_id, c.create_key_package().unwrap());
        let incoming = b.process_incoming(room_id.clone(), outcome.commit).unwrap();
        assert!(matches!(incoming.kind, IncomingKind::Handshake));
        assert_eq!(incoming.added.len(), 1);
        assert_eq!(incoming.added[0].identity, "carol2-device");
        assert!(incoming.removed.is_empty());

        c.join_group(outcome.welcome).unwrap();
        assert_eq!(c.list_members(room_id).unwrap().len(), 3);
    }

    #[test]
    fn remove_member_round_trip() {
        let (mut a, _) = client("owner3");
        let (mut b, _) = client("leaver3");
        let (mut c, _) = client("stayer3");

        let room_id = b"remove-room".to_vec();
        a.create_group(room_id.clone()).unwrap();

        let out_b = add_merged(&mut a, &room_id, b.create_key_package().unwrap());
        b.join_group(out_b.welcome).unwrap();

        let out_c = add_merged(&mut a, &room_id, c.create_key_package().unwrap());
        b.process_incoming(room_id.clone(), out_c.commit).unwrap();
        c.join_group(out_c.welcome).unwrap();

        // A removes B — remaining C sees the removed leaf in the commit
        let removed = remove_merged(&mut a, &room_id, "leaver3-device");
        assert!(removed.epoch >= 3);

        let incoming = c
            .process_incoming(room_id.clone(), removed.commit.clone())
            .unwrap();
        assert!(matches!(incoming.kind, IncomingKind::Handshake));
        assert_eq!(incoming.removed.len(), 1);
        assert_eq!(incoming.removed[0].identity, "leaver3-device");

        let ids: Vec<String> = c
            .list_members(room_id.clone())
            .unwrap()
            .into_iter()
            .map(|m| m.identity)
            .collect();
        assert!(!ids.contains(&"leaver3-device".to_string()));

        // The removed B, processing the commit, is pushed out of the group
        // — no sending afterwards
        b.process_incoming(room_id.clone(), removed.commit).unwrap();
        assert!(b.encrypt(room_id.clone(), b"ghost".to_vec()).is_err());

        // Removing a nonexistent identity or yourself is an error (my
        // departure is deleted by a remaining device)
        assert!(a
            .remove_member(room_id.clone(), "nobody-device".to_string())
            .is_err());
        assert!(a
            .remove_member(room_id, "owner3-device".to_string())
            .is_err());
    }

    #[test]
    fn staged_commit_clear_allows_retry() {
        // A commit the server rejected with stale_epoch is cleared then
        // re-staged — no local fork
        let (mut a, _) = client("stager");
        let (mut b, _) = client("stagee");
        let room_id = b"staged-room".to_vec();
        a.create_group(room_id.clone()).unwrap();

        let kp = b.create_key_package().unwrap();
        let first = a.add_member(room_id.clone(), kp).unwrap();
        assert_eq!(first.epoch, 1);
        // While staged, new add/removes are refused — prevents ownerless
        // merges
        assert!(a
            .add_member(room_id.clone(), b.create_key_package().unwrap())
            .is_err());
        // Pretend it was rejected and discard — epoch unchanged
        a.clear_pending(room_id.clone()).unwrap();
        assert_eq!(a.group_epoch(room_id.clone()).unwrap(), 0);

        // Retry — stage again with a fresh KeyPackage → accept → merge
        let kp2 = b.create_key_package().unwrap();
        let second = a.add_member(room_id.clone(), kp2).unwrap();
        assert_eq!(a.merge_pending(room_id.clone()).unwrap(), 1);
        b.join_group(second.welcome).unwrap();
        assert_eq!(b.list_members(room_id).unwrap().len(), 2);
    }

    #[test]
    fn own_merged_commit_echo_is_own_not_an_error() {
        // The approver scenario: create an Add commit, finish the merge,
        // then the same commit returns as an echo on the server relay. It
        // must be reported as own, not an error, so the room's first
        // message doesn't render as "message unavailable" (real device 2/2)
        let (mut owner, _) = client("echo-owner");
        let (mut guest, _) = client("echo-guest");
        let room_id = b"room-echo".to_vec();
        owner.create_group(room_id.clone()).unwrap();

        let out = add_merged(&mut owner, &room_id, guest.create_key_package().unwrap());
        guest.join_group(out.welcome).unwrap();

        let incoming = owner.process_incoming(room_id.clone(), out.commit).unwrap();
        assert!(matches!(incoming.kind, IncomingKind::Own));

        // After the echo, group state and members are unchanged
        assert_eq!(owner.list_members(room_id).unwrap().len(), 2);
    }

    #[test]
    fn key_package_info_matches_registered_signing_key() {
        // The approver's verification equation: kp's (identity, signature
        // key) == (requesting device_id, registry key)
        let (mut a, _) = client("kpinfo");
        let kp = a.create_key_package().unwrap();

        let info = a.key_package_info(kp).unwrap();
        assert_eq!(info.identity, "kpinfo-device");
        assert_eq!(info.signature_key, a.signing_public_key().unwrap());
        assert_eq!(a.signing_public_key().unwrap().len(), 32);

        // Garbage bytes fail validation = a reject reason
        assert!(a.key_package_info(b"garbage".to_vec()).is_err());
    }

    #[test]
    fn remove_member_removes_every_leaf_with_the_same_identity() {
        // Two leaves of the same identity (device) = the stale-leaf
        // scenario — all of them must go
        let (mut owner, _) = client("dup-owner");
        let mut stale_one =
            MlsClient::open(test_dir("dup-one"), "duplicate-device".to_string()).unwrap();
        let mut stale_two =
            MlsClient::open(test_dir("dup-two"), "duplicate-device".to_string()).unwrap();

        let room_id = b"duplicate-room".to_vec();
        owner.create_group(room_id.clone()).unwrap();

        let first = add_merged(&mut owner, &room_id, stale_one.create_key_package().unwrap());
        stale_one.join_group(first.welcome).unwrap();

        let second = add_merged(&mut owner, &room_id, stale_two.create_key_package().unwrap());
        stale_two.join_group(second.welcome).unwrap();

        assert_eq!(
            owner
                .list_members(room_id.clone())
                .unwrap()
                .iter()
                .filter(|m| m.identity == "duplicate-device")
                .count(),
            2
        );

        remove_merged(&mut owner, &room_id, "duplicate-device");
        assert!(owner
            .list_members(room_id)
            .unwrap()
            .iter()
            .all(|m| m.identity != "duplicate-device"));
    }

    #[test]
    fn update_path_rotation_is_not_a_membership_diff() {
        // An Update commit rotating only the encryption key — showing up
        // in the member diff would be a false positive
        let (mut a, _) = client("update-a");
        let (mut b, _) = client("update-b");
        let room_id = b"update-room".to_vec();

        a.create_group(room_id.clone()).unwrap();
        let added = add_merged(&mut a, &room_id, b.create_key_package().unwrap());
        b.join_group(added.welcome).unwrap();

        let commit = a
            .locked(|c| {
                let mut group = c.load_group(&room_id)?;
                let commit = group
                    .self_update(&c.provider, &c.signer, LeafNodeParameters::default())
                    .map_err(|e| anyhow!("self update: {e:?}"))?
                    .into_commit()
                    .tls_serialize_detached()
                    .context("self update serialize")?;
                group
                    .merge_pending_commit(&c.provider)
                    .map_err(|e| anyhow!("merge self update: {e:?}"))?;
                Ok(commit)
            })
            .unwrap();

        let incoming = b.process_incoming(room_id, commit).unwrap();
        assert!(matches!(incoming.kind, IncomingKind::Handshake));
        assert!(incoming.added.is_empty());
        assert!(incoming.removed.is_empty());
    }

    #[test]
    fn external_init_gate_rejects_without_advancing() {
        let (mut a, _) = client("gatekeeper");
        let (mut x, _) = client("intruder");

        let room_id = b"gated-room".to_vec();
        let group_info = a.create_group(room_id.clone()).unwrap();

        a.set_reject_external_init(true);
        // The gate must survive a reload (the flock contract) — wedge
        // another operation in between
        let _ = a.encrypt(room_id.clone(), b"warmup".to_vec()).unwrap();

        let joined = x.join_by_external_commit(group_info).unwrap();
        let incoming = a
            .process_incoming(room_id.clone(), joined.commit)
            .unwrap();
        assert!(matches!(incoming.kind, IncomingKind::RejectedExternalJoin));

        // No merge happened — epoch unchanged, members unchanged
        assert_eq!(a.group_epoch(room_id.clone()).unwrap(), 0);
        assert_eq!(a.list_members(room_id.clone()).unwrap().len(), 1);

        // With the gate off, the old behavior (covered by
        // external_commit_join)
        a.set_reject_external_init(false);
        let ct = a.encrypt(room_id, b"still alive".to_vec()).unwrap();
        assert!(!ct.is_empty());
    }

    // ── agent ingest (feature "agent-cli") — agent-mls-v1 §1 ──────────

    /// Batch ingest: decrypt, cursor, and receipt in one transaction —
    /// re-calls return the stored plaintext without consuming the ratchet
    /// again (prevents plaintext loss in the kill window)
    #[cfg(feature = "agent-cli")]
    #[test]
    fn agent_ingest_replay_and_ack() {
        let (mut a, _) = client("ing_a");
        let (mut b, b_dir) = client("ing_b");

        let room = b"room-ingest-uuid".to_vec();
        a.create_group(room.clone()).unwrap();
        let kp = b.create_key_package().unwrap();
        let out = add_merged(&mut a, &room, kp);
        b.join_group(out.welcome).unwrap();

        let ct1 = a.encrypt(room.clone(), b"one".to_vec()).unwrap();
        let ct2 = a.encrypt(room.clone(), b"two".to_vec()).unwrap();

        // First batch — two items processed, cursor advanced
        let r = b
            .agent_ingest(room.clone(), vec![(10, ct1.clone()), (12, ct2.clone())])
            .unwrap();
        assert_eq!(r.len(), 2);
        assert_eq!(r[0].incoming.as_ref().unwrap().plaintext.as_deref(), Some(b"one".as_ref()));
        assert_eq!(r[1].incoming.as_ref().unwrap().plaintext.as_deref(), Some(b"two".as_ref()));
        assert_eq!(b.agent_cursor(room.clone()).unwrap().0, Some(12));
        // Un-acked receipts exist, so the resume point sits below them
        // (P0: receipt delivery guarantee)
        assert_eq!(b.agent_cursor(room.clone()).unwrap().1, Some(9));

        // Crash-restart simulation: a new client refeeds the same seqs —
        // not a ratchet error; the stored receipts come back as-is
        drop(b);
        let mut b2 = MlsClient::open(b_dir, "ing_b-device".into()).unwrap();
        let r2 = b2.agent_ingest(room.clone(), vec![(10, ct1), (12, ct2)]).unwrap();
        assert!(r2.iter().all(|o| o.replayed && !o.pruned));
        assert_eq!(
            r2[0].incoming.as_ref().unwrap().plaintext.as_deref(),
            Some(b"one".as_ref())
        );

        // After ledger finalization (ack) the receipts are cleared and a
        // refeed comes back pruned — the cursor stays, so reprocessing
        // (ratchet consumption) still never happens
        assert_eq!(b2.agent_ingest_ack(room.clone(), 12).unwrap(), 2);
        let ct3 = a.encrypt(room.clone(), b"three".to_vec()).unwrap();
        let r3 = b2
            .agent_ingest(room.clone(), vec![(12, vec![1, 2, 3]), (15, ct3)])
            .unwrap();
        assert!(r3[0].replayed && r3[0].pruned);
        assert_eq!(
            r3[1].incoming.as_ref().unwrap().plaintext.as_deref(),
            Some(b"three".as_ref())
        );
        let (cur, resume) = b2.agent_cursor(room.clone()).unwrap();
        assert_eq!(cur, Some(15));
        // Only seq 15's receipt remains → resume point 14 (back to the
        // cursor after ack)
        assert_eq!(resume, Some(14));
        b2.agent_ingest_ack(room.clone(), 15).unwrap();
        assert_eq!(b2.agent_cursor(room.clone()).unwrap().1, Some(15));
        // skip: cursor advances only — ratchet untouched
        b2.agent_ingest_skip(room.clone(), 20).unwrap();
        assert_eq!(b2.agent_cursor(room).unwrap().0, Some(20));
    }

    /// Stops at the failed seq — the cursor covers only up to just before
    /// the failure; earlier successes are committed
    #[cfg(feature = "agent-cli")]
    #[test]
    fn agent_ingest_stops_at_failure() {
        let (mut a, _) = client("stop_a");
        let (mut b, _) = client("stop_b");

        let room = b"room-stop-uuid".to_vec();
        a.create_group(room.clone()).unwrap();
        let kp = b.create_key_package().unwrap();
        let out = add_merged(&mut a, &room, kp);
        b.join_group(out.welcome).unwrap();

        let ok_ct = a.encrypt(room.clone(), b"fine".to_vec()).unwrap();
        let r = b
            .agent_ingest(
                room.clone(),
                vec![(1, ok_ct), (2, b"garbage-not-mls".to_vec()), (3, vec![9, 9])],
            )
            .unwrap();
        // One success + stop at one failure — seq 3 is never attempted
        assert_eq!(r.len(), 2);
        assert!(r[0].error.is_none());
        assert!(r[1].error.is_some());
        assert_eq!(b.agent_cursor(room.clone()).unwrap().0, Some(1));

        // A descending batch is refused — the first item is below the
        // cursor (replay), the second goes backwards
        let r2 = b.agent_ingest(room, vec![(1, vec![1]), (0, vec![2])]).unwrap();
        assert!(r2[0].replayed);
        assert!(r2[1].error.as_deref().unwrap_or("").contains("ascending"));
    }

    /// The batch stops at our own Remove, CAS encryption rejects roster
    /// changes, and delete-group folds the KAGENT residue (cursor +
    /// receipts) too (impl /133)
    #[cfg(feature = "agent-cli")]
    #[test]
    fn agent_self_remove_cas_and_purge() {
        let (mut a, _) = client("sr_a");
        let (mut b, _) = client("sr_b");

        let room = b"room-selfremove".to_vec();
        a.create_group(room.clone()).unwrap();
        let kp = b.create_key_package().unwrap();
        let out = add_merged(&mut a, &room, kp);
        b.join_group(out.welcome).unwrap();

        // CAS: reject when the epoch differs from verification time
        let epoch = b.group_epoch(room.clone()).unwrap();
        assert!(b.agent_encrypt(room.clone(), epoch, b"ok".to_vec()).is_ok());
        let err = b
            .agent_encrypt(room.clone(), epoch + 7, b"nope".to_vec())
            .unwrap_err();
        assert!(format!("{err:#}").contains("roster_changed"));

        // Our own Remove commit + items after it — the batch stops at the
        // Remove
        let rm = remove_merged(&mut a, &room, "sr_b-device");
        let after = a.encrypt(room.clone(), b"after".to_vec()).unwrap();
        let r = b
            .agent_ingest(room.clone(), vec![(5, rm.commit), (6, after)])
            .unwrap();
        assert_eq!(r.len(), 1);
        assert!(r[0]
            .incoming
            .as_ref()
            .unwrap()
            .removed
            .iter()
            .any(|m| m.identity == "sr_b-device"));

        // delete-group folds the KAGENT cursor and receipts too
        b.delete_group(room.clone()).unwrap();
        let (cur, resume) = b.agent_cursor(room).unwrap();
        assert_eq!((cur, resume), (None, None));
    }
}
