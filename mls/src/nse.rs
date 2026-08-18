//! Single-notification decryption — the iOS NSE (Swift, C ABI) and the
//! Android FCM service (Kotlin, JNI) call the same `decrypt_str` through
//! different shells.
//!
//! Unlike the FRB (Dart) surface, this is all "decrypt one notification".
//! It shares the app's state file (iOS: app-group container, separate
//! process; Android: another thread of the same process), and thanks to
//! `MlsClient`'s flock → reload → save contract, both sides can each
//! decrypt the same message and the state still converges.
//!
//! Return contract: success = plaintext payload JSON; failure or
//! non-displayable messages (commits etc.) = null. On null the caller
//! keeps the server-provided default body ("New message from ~") —
//! graceful degradation.

use std::ffi::{c_char, CStr, CString};

use base64::Engine;

use crate::api::mls::{IncomingKind, MlsClient};

/// Shared core — used by both the C ABI (iOS) and JNI (Android).
pub(crate) fn decrypt_str(dir: &str, group_id: &str, ct_base64: &str) -> Option<String> {
    let ct = base64::engine::general_purpose::STANDARD
        .decode(ct_base64)
        .ok()?;

    let mut client = MlsClient::open_existing(dir.to_string()).ok()?;
    let incoming = client
        .process_incoming(group_id.as_bytes().to_vec(), ct)
        .ok()?;

    match incoming.kind {
        IncomingKind::Application => String::from_utf8(incoming.plaintext?).ok(),
        // commit/proposal: advancing state IS the handling — nothing to
        // display. own: an echo of this device's send — no notification
        // arrives for it, but ignore defensively. A rejected external
        // join has nothing to display either — degrade to the server's
        // default body
        IncomingKind::Handshake | IncomingKind::Own | IncomingKind::RejectedExternalJoin => None,
    }
}

/// # Safety
/// All three pointers must be valid NUL-terminated UTF-8 strings.
#[no_mangle]
pub unsafe extern "C" fn klatalk_nse_decrypt(
    dir: *const c_char,
    group_id: *const c_char,
    ct_base64: *const c_char,
) -> *mut c_char {
    let result = std::panic::catch_unwind(|| decrypt_inner(dir, group_id, ct_base64));
    match result {
        Ok(Some(json)) => CString::new(json)
            .map(CString::into_raw)
            .unwrap_or(std::ptr::null_mut()),
        _ => std::ptr::null_mut(),
    }
}

/// # Safety
/// Only pass pointers returned by `klatalk_nse_decrypt` (null allowed,
/// never twice).
#[no_mangle]
pub unsafe extern "C" fn klatalk_nse_free(ptr: *mut c_char) {
    if !ptr.is_null() {
        drop(unsafe { CString::from_raw(ptr) });
    }
}

fn decrypt_inner(
    dir: *const c_char,
    group_id: *const c_char,
    ct_base64: *const c_char,
) -> Option<String> {
    decrypt_str(c_str(dir)?, c_str(group_id)?, c_str(ct_base64)?)
}

fn c_str<'a>(ptr: *const c_char) -> Option<&'a str> {
    if ptr.is_null() {
        return None;
    }
    unsafe { CStr::from_ptr(ptr) }.to_str().ok()
}

// ---- Android JNI (called by FirebaseMessagingService) ----

#[cfg(target_os = "android")]
mod android {
    use jni::objects::{JClass, JString};
    use jni::sys::jstring;
    use jni::JNIEnv;

    /// Kotlin `MlsNative.nseDecrypt` — success = plaintext payload JSON,
    /// failure = null.
    #[no_mangle]
    pub extern "system" fn Java_com_klatalk_klatalk_MlsNative_nseDecrypt<'local>(
        mut env: JNIEnv<'local>,
        _class: JClass<'local>,
        dir: JString<'local>,
        group_id: JString<'local>,
        ct_base64: JString<'local>,
    ) -> jstring {
        let decrypted = (|| {
            let dir: String = env.get_string(&dir).ok()?.into();
            let group_id: String = env.get_string(&group_id).ok()?.into();
            let ct: String = env.get_string(&ct_base64).ok()?.into();
            std::panic::catch_unwind(|| super::decrypt_str(&dir, &group_id, &ct))
                .ok()
                .flatten()
        })();

        match decrypted {
            Some(json) => env
                .new_string(json)
                .map(|s| s.into_raw())
                .unwrap_or(std::ptr::null_mut()),
            None => std::ptr::null_mut(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn c_api_round_trip() {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let host_dir = std::env::temp_dir().join(format!("klatalk_nse_host_{nanos}"));
        let nse_dir = std::env::temp_dir().join(format!("klatalk_nse_ext_{nanos}"));

        // The "app" role: create the group in the guest state directory
        let mut host =
            MlsClient::open(host_dir.to_string_lossy().into_owned(), "host-dev".into()).unwrap();
        let mut guest =
            MlsClient::open(nse_dir.to_string_lossy().into_owned(), "guest-dev".into()).unwrap();

        let room = b"room-uuid".to_vec();
        // Setup uses the external-join path — state the gate policy
        // explicitly so the test is deterministic regardless of the
        // compile-time default (§8-4 build)
        host.set_reject_external_init(false);
        let group_info = host.create_group(room.clone()).unwrap();
        let joined = guest.join_by_external_commit(group_info).unwrap();
        host.process_incoming(room.clone(), joined.commit).unwrap();

        let payload_json = r#"{"type":"text","text":"NSE round trip"}"#;
        let ct = host
            .encrypt(room.clone(), payload_json.as_bytes().to_vec())
            .unwrap();

        // The "NSE" role: decrypt from the same state directory via the C API
        let dir_c = CString::new(nse_dir.to_string_lossy().into_owned()).unwrap();
        let gid_c = CString::new("room-uuid").unwrap();
        let ct_c =
            CString::new(base64::engine::general_purpose::STANDARD.encode(&ct)).unwrap();

        let out = unsafe { klatalk_nse_decrypt(dir_c.as_ptr(), gid_c.as_ptr(), ct_c.as_ptr()) };
        assert!(!out.is_null());
        let json = unsafe { CStr::from_ptr(out) }.to_str().unwrap().to_owned();
        unsafe { klatalk_nse_free(out) };
        assert_eq!(json, payload_json);

        // Feeding the same message again (ratchet consumed) yields null —
        // graceful degradation
        let out2 = unsafe { klatalk_nse_decrypt(dir_c.as_ptr(), gid_c.as_ptr(), ct_c.as_ptr()) };
        assert!(out2.is_null());
    }
}
