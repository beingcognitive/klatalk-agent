//! Invite quiz sealing — RFC 9180 HPKE single-shot seal/open.
//!
//! Canon: docs/mls-phase-a-v1.md §5, docs/invite-quiz-v1.md.
//!
//! - Issuing device: generates a keypair per link → the public key rides
//!   the link fragment (`#q=`), the secret key stays local in my_invites.
//!   The server sees neither.
//! - Joiner: seals the normalized answer with the issuing device's public
//!   key and attaches it to the join request.
//! - aad = the joiner's key_package bytes — the sealed answer is bound to
//!   that join request. If the server splices another joiner's sealed
//!   correct answer onto this one, open fails (splicing blocked).
//! - Pure functions, independent of the MLS state file — never touch the
//!   flock/reload path.

use anyhow::{anyhow, Result};
use openmls::prelude::tls_codec::{Deserialize, Serialize};
use openmls_rust_crypto::OpenMlsRustCrypto;
use openmls_traits::crypto::OpenMlsCrypto;
use openmls_traits::random::OpenMlsRand;
use openmls_traits::types::{
    HpkeAeadType, HpkeCiphertext, HpkeConfig, HpkeKdfType, HpkeKemType,
};
use openmls_traits::OpenMlsProvider;

/// Same family as the MLS ciphersuite (MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519).
const HPKE_CONFIG: HpkeConfig = HpkeConfig(
    HpkeKemType::DhKem25519,
    HpkeKdfType::HkdfSha256,
    HpkeAeadType::AesGcm128,
);

/// Domain separation — never compatible with HPKE sealing for any other
/// purpose (backups etc.).
const INFO: &[u8] = b"klatalk-invite-quiz-v1";

/// The sealing keypair of a single link. The public key goes out in the
/// fragment; the secret key stays local.
pub struct QuizKeyPair {
    pub public_key: Vec<u8>,
    pub secret_key: Vec<u8>,
}

/// Answer normalization — NFC → lowercase → strip all whitespace. **Both
/// the issuing device and the joiner use this one function** — if
/// normalization diverges, a correct answer becomes a wrong one. Dart has
/// no Unicode normalization, so it lives here (single implementation).
/// "Spacing doesn't count."
pub fn quiz_normalize(answer: String) -> String {
    use unicode_normalization::UnicodeNormalization;
    answer
        .nfc()
        .collect::<String>()
        .to_lowercase()
        .chars()
        .filter(|c| !c.is_whitespace())
        .collect()
}

/// Once per invite link issuance — generates an X25519 HPKE keypair.
pub fn quiz_keypair() -> Result<QuizKeyPair> {
    let provider = OpenMlsRustCrypto::default();
    let ikm = provider
        .rand()
        .random_vec(32)
        .map_err(|e| anyhow!("ikm: {e:?}"))?;
    let pair = provider
        .crypto()
        .derive_hpke_keypair(HPKE_CONFIG, &ikm)
        .map_err(|e| anyhow!("derive: {e:?}"))?;
    Ok(QuizKeyPair {
        public_key: pair.public,
        secret_key: pair.private.to_vec(),
    })
}

/// Joiner — seals the normalized answer with the issuing device's public
/// key. The output is a single TLS-serialized HPKECiphertext(kem_output,
/// ciphertext) byte string.
pub fn quiz_seal(recipient_public_key: Vec<u8>, aad: Vec<u8>, answer: Vec<u8>) -> Result<Vec<u8>> {
    let provider = OpenMlsRustCrypto::default();
    let ct = provider
        .crypto()
        .hpke_seal(HPKE_CONFIG, &recipient_public_key, INFO, &aad, &answer)
        .map_err(|e| anyhow!("seal: {e:?}"))?;
    ct.tls_serialize_detached()
        .map_err(|e| anyhow!("serialize: {e:?}"))
}

/// Issuing device — opens the seal and returns the answer plaintext.
/// Errors when the secret key or aad mismatch (callers must not
/// distinguish the reason — a wrong answer and a forgery are the same
/// failure).
pub fn quiz_open(secret_key: Vec<u8>, aad: Vec<u8>, sealed: Vec<u8>) -> Result<Vec<u8>> {
    let provider = OpenMlsRustCrypto::default();
    let ct = HpkeCiphertext::tls_deserialize_exact(sealed.as_slice())
        .map_err(|e| anyhow!("deserialize: {e:?}"))?;
    provider
        .crypto()
        .hpke_open(HPKE_CONFIG, &ct, &secret_key, INFO, &aad)
        .map_err(|e| anyhow!("open: {e:?}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        let kp = quiz_keypair().unwrap();
        let sealed = quiz_seal(kp.public_key.clone(), b"kp-bytes".to_vec(), "우리집 강아지".as_bytes().to_vec()).unwrap();
        let opened = quiz_open(kp.secret_key, b"kp-bytes".to_vec(), sealed).unwrap();
        assert_eq!(opened, "우리집 강아지".as_bytes());
    }

    #[test]
    fn wrong_key_fails() {
        let kp1 = quiz_keypair().unwrap();
        let kp2 = quiz_keypair().unwrap();
        let sealed = quiz_seal(kp1.public_key, vec![], b"answer".to_vec()).unwrap();
        assert!(quiz_open(kp2.secret_key, vec![], sealed).is_err());
    }

    #[test]
    fn aad_mismatch_fails() {
        // The splicing scenario: the server detaches the sealed correct
        // answer from another join request and reattaches it here
        let kp = quiz_keypair().unwrap();
        let sealed = quiz_seal(kp.public_key, b"joiner-A-kp".to_vec(), b"answer".to_vec()).unwrap();
        assert!(quiz_open(kp.secret_key, b"joiner-B-kp".to_vec(), sealed).is_err());
    }

    #[test]
    fn tampered_sealed_fails() {
        let kp = quiz_keypair().unwrap();
        let mut sealed = quiz_seal(kp.public_key, vec![], b"answer".to_vec()).unwrap();
        let last = sealed.len() - 1;
        sealed[last] ^= 0x01;
        assert!(quiz_open(kp.secret_key, vec![], sealed).is_err());
    }

    #[test]
    fn normalize_answer() {
        // Strips whitespace (fullwidth included) + lowercase + NFC (jamo
        // composition)
        assert_eq!(quiz_normalize("  제주 도  ".into()), "제주도");
        assert_eq!(quiz_normalize("Jeju Island".into()), "jejuisland");
        // NFD (decomposed jamo) input yields the same bytes — "제주도"
        // written out as separate jamo
        let nfd = "\u{110C}\u{1166}\u{110C}\u{116E}\u{1103}\u{1169}";
        assert_eq!(quiz_normalize(nfd.into()), "제주도");
        assert_eq!(quiz_normalize("답\u{3000}이야".into()), "답이야");
    }

    #[test]
    fn keypair_is_unique_per_link() {
        let a = quiz_keypair().unwrap();
        let b = quiz_keypair().unwrap();
        assert_ne!(a.public_key, b.public_key);
        assert_eq!(a.public_key.len(), 32); // X25519
    }
}
