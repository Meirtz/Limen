//! ed25519 agent identity (opt-in).
//!
//! Upgrades the identity primitive from an *asserted* plaintext label to a
//! *cryptographically-rooted* one. A registered agent signs its lease acquisition
//! and the server verifies the signature against the registered public key; the
//! lease id then acts as a bearer capability for the writes/renew/release that
//! follow. Unregistered labels keep the plaintext advisory path, so the simple
//! workflow is unchanged.
//!
//! Keys are raw 32-byte ed25519 values, hex-encoded. The private "key" stored on
//! disk is the 32-byte seed.

use crate::store::StoreError;
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};

/// The canonical message an agent signs to acquire a lease. Both the agent (when
/// signing) and the server (when verifying) must build it identically.
pub fn acquire_message(path_pattern: &str, intent: &str, agent_label: &str) -> String {
    format!("limen.acquire\n{path_pattern}\n{intent}\n{agent_label}")
}

/// Generate a new keypair: `(private_seed_hex, public_key_hex)`, each 32 bytes.
pub fn generate_keypair() -> (String, String) {
    let mut seed = [0u8; 32];
    getrandom::getrandom(&mut seed).expect("OS RNG unavailable");
    let sk = SigningKey::from_bytes(&seed);
    let pk = sk.verifying_key();
    (hex_encode(&seed), hex_encode(pk.as_bytes()))
}

/// Sign `message` with a 32-byte private seed (hex); returns the signature hex.
pub fn sign(private_seed_hex: &str, message: &str) -> Result<String, StoreError> {
    let seed = decode_fixed::<32>(private_seed_hex)?;
    let sk = SigningKey::from_bytes(&seed);
    Ok(hex_encode(&sk.sign(message.as_bytes()).to_bytes()))
}

/// Verify `signature_hex` over `message` against `public_key_hex`.
pub fn verify(public_key_hex: &str, message: &str, signature_hex: &str) -> Result<(), StoreError> {
    let vk = verifying_key(public_key_hex)?;
    // A malformed signature is an invalid signature, not a key problem.
    let sig_bytes = decode_fixed::<64>(signature_hex).map_err(|_| StoreError::SignatureInvalid)?;
    let sig = Signature::from_bytes(&sig_bytes);
    vk.verify(message.as_bytes(), &sig)
        .map_err(|_| StoreError::SignatureInvalid)
}

/// Validate that `public_key_hex` is a well-formed ed25519 public key.
pub fn validate_public_key(public_key_hex: &str) -> Result<(), StoreError> {
    verifying_key(public_key_hex).map(|_| ())
}

fn verifying_key(public_key_hex: &str) -> Result<VerifyingKey, StoreError> {
    let bytes = decode_fixed::<32>(public_key_hex)?;
    VerifyingKey::from_bytes(&bytes)
        .map_err(|_| StoreError::InvalidKey("not a valid ed25519 public key".into()))
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

fn decode_fixed<const N: usize>(hex: &str) -> Result<[u8; N], StoreError> {
    if hex.len() != N * 2 {
        return Err(StoreError::InvalidKey(format!(
            "expected {} hex characters, got {}",
            N * 2,
            hex.len()
        )));
    }
    let mut out = [0u8; N];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&hex[i * 2..i * 2 + 2], 16)
            .map_err(|_| StoreError::InvalidKey("non-hex character".into()))?;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sign_then_verify_roundtrip() {
        let (sk, pk) = generate_keypair();
        let msg = acquire_message("src/auth/", "write", "agent-A");
        let sig = sign(&sk, &msg).unwrap();
        assert!(verify(&pk, &msg, &sig).is_ok());
    }

    #[test]
    fn verify_rejects_wrong_message_or_key() {
        let (sk, pk) = generate_keypair();
        let (_sk2, pk2) = generate_keypair();
        let msg = acquire_message("src/", "write", "a");
        let sig = sign(&sk, &msg).unwrap();
        // tampered message
        assert!(verify(&pk, &acquire_message("src/", "write", "b"), &sig).is_err());
        // wrong key
        assert!(verify(&pk2, &msg, &sig).is_err());
        // garbage signature
        assert!(verify(&pk, &msg, "00").is_err());
    }

    #[test]
    fn validate_public_key_checks_form() {
        let (_sk, pk) = generate_keypair();
        assert!(validate_public_key(&pk).is_ok());
        assert!(validate_public_key("xyz").is_err());
    }
}
