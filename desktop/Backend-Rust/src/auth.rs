// Casdoor OIDC — id_token verification.
// Ports backend/utils/oidc.py: verify RS256 JWTs against Casdoor's JWKS,
// audience = CASDOOR_CLIENT_ID, uid = `sub` claim.

use axum::{
    async_trait,
    extract::FromRequestParts,
    http::{request::Parts, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use jsonwebtoken::{decode, decode_header, DecodingKey, Validation};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;

/// Casdoor JWKS-backed id_token verifier.
pub struct CasdoorAuth {
    /// Cached signing keys (kid -> DecodingKey)
    keys: Arc<RwLock<HashMap<String, DecodingKey>>>,
    /// HTTP client for fetching the JWKS
    client: Client,
    /// JWKS URLs to try in order (internal cluster URL first, then public).
    jwks_urls: Vec<String>,
    /// Expected `aud` claim — the Casdoor OAuth client id.
    audience: String,
}

/// Claims we read from a Casdoor id_token.
/// `aud`/`exp` are validated internally by jsonwebtoken (via `Validation`),
/// so they're intentionally not deserialized here (Casdoor may emit `aud` as
/// either a string or an array — letting the library handle it avoids a
/// string-vs-array deserialization mismatch).
#[derive(Debug, Deserialize)]
#[allow(dead_code)]
pub struct CasdoorClaims {
    /// Subject — the uid (`{org}/{username}` in Casdoor)
    pub sub: String,
    /// Email (optional)
    pub email: Option<String>,
    /// Email verified
    pub email_verified: Option<bool>,
    /// Display name (optional)
    pub name: Option<String>,
    /// Username fallback when `name` is absent
    pub preferred_username: Option<String>,
}

/// JWKS response (Casdoor `/.well-known/jwks`)
#[derive(Debug, Deserialize)]
struct JwksResponse {
    keys: Vec<JwkKey>,
}

#[derive(Debug, Deserialize)]
struct JwkKey {
    kid: String,
    n: String,
    e: String,
    kty: String,
    #[allow(dead_code)]
    alg: Option<String>,
}

/// Auth error response.
///
/// Status codes:
/// - `trial_expired` → 402 Payment Required (so clients can distinguish paywall
///   from auth failure and show the upgrade UI)
/// - any other error string → 401 Unauthorized
#[derive(Debug, Serialize)]
pub struct AuthError {
    pub error: String,
    pub message: String,
}

impl IntoResponse for AuthError {
    fn into_response(self) -> Response {
        let status = if self.error == "trial_expired" {
            StatusCode::PAYMENT_REQUIRED
        } else if self.error == "byok_validation_failed" {
            StatusCode::FORBIDDEN
        } else {
            StatusCode::UNAUTHORIZED
        };
        (status, Json(self)).into_response()
    }
}

impl CasdoorAuth {
    /// Create a new Casdoor OIDC verifier.
    ///
    /// `endpoint` is the public Casdoor URL (e.g. https://door.spangled-kettle.ts.net);
    /// `internal_url` (optional) is preferred for in-cluster JWKS fetches;
    /// `audience` is the Casdoor client id expected in the token's `aud`.
    pub fn new(endpoint: String, internal_url: Option<String>, audience: String) -> Self {
        let mut jwks_urls = Vec::new();
        if let Some(internal) = internal_url {
            let internal = internal.trim_end_matches('/');
            if !internal.is_empty() {
                jwks_urls.push(format!("{}/.well-known/jwks", internal));
            }
        }
        jwks_urls.push(format!("{}/.well-known/jwks", endpoint.trim_end_matches('/')));

        Self {
            keys: Arc::new(RwLock::new(HashMap::new())),
            client: Client::new(),
            jwks_urls,
            audience,
        }
    }

    /// Fetch the JWKS from Casdoor, trying the internal URL first then the public
    /// one (mirrors the internal/external preference in backend/utils/oidc.py).
    pub async fn refresh_keys(&self) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let mut last_err: Option<Box<dyn std::error::Error + Send + Sync>> = None;

        for url in &self.jwks_urls {
            let fetched = self
                .client
                .get(url)
                .send()
                .await
                .and_then(|r| r.error_for_status());
            match fetched {
                Ok(resp) => match resp.json::<JwksResponse>().await {
                    Ok(jwks) => {
                        let mut keys = self.keys.write().await;
                        keys.clear();
                        for key in jwks.keys {
                            if key.kty == "RSA" {
                                if let Ok(decoding_key) = DecodingKey::from_rsa_components(&key.n, &key.e) {
                                    keys.insert(key.kid, decoding_key);
                                }
                            }
                        }
                        tracing::info!("Refreshed {} Casdoor JWKS keys from {}", keys.len(), url);
                        return Ok(());
                    }
                    Err(e) => last_err = Some(Box::new(e)),
                },
                Err(e) => {
                    tracing::warn!("Casdoor JWKS fetch failed for {}: {}", url, e);
                    last_err = Some(Box::new(e));
                }
            }
        }

        Err(last_err.unwrap_or_else(|| "no JWKS URLs configured".into()))
    }

    /// Verify a Casdoor id_token and extract (uid, name, email).
    pub async fn verify_token(&self, token: &str) -> Result<(String, Option<String>, Option<String>), AuthError> {
        // Decode header to get kid
        let header = decode_header(token).map_err(|e| AuthError {
            error: "invalid_token".to_string(),
            message: format!("Failed to decode token header: {}", e),
        })?;

        let kid = header.kid.ok_or_else(|| AuthError {
            error: "invalid_token".to_string(),
            message: "Token missing kid header".to_string(),
        })?;

        // Get the key for this kid
        let keys = self.keys.read().await;
        let key = keys.get(&kid).ok_or_else(|| AuthError {
            error: "invalid_token".to_string(),
            message: format!("Unknown key id: {}", kid),
        })?;

        // Match the Python verifier (oidc.py): RS256 + audience, no issuer check.
        let mut validation = Validation::new(jsonwebtoken::Algorithm::RS256);
        validation.set_audience(&[&self.audience]);

        // Decode and validate token
        let token_data = decode::<CasdoorClaims>(token, key, &validation).map_err(|e| AuthError {
            error: "invalid_token".to_string(),
            message: format!("Token validation failed: {}", e),
        })?;

        let claims = token_data.claims;
        let name = claims.name.or(claims.preferred_username);
        Ok((claims.sub, name, claims.email))
    }
}

/// Authenticated user extractor for Axum
/// Usage: async fn handler(user: AuthUser) -> impl IntoResponse { ... }
#[derive(Debug, Clone)]
pub struct AuthUser {
    pub uid: String,
    pub name: Option<String>,
    pub email: Option<String>,
}

/// Extension to store the Casdoor auth verifier in request
#[derive(Clone)]
pub struct CasdoorAuthExt(pub Arc<CasdoorAuth>);

#[async_trait]
impl<S> FromRequestParts<S> for AuthUser
where
    S: Send + Sync,
{
    type Rejection = AuthError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        // Get Authorization header
        let auth_header = parts
            .headers
            .get("Authorization")
            .and_then(|h| h.to_str().ok())
            .ok_or_else(|| AuthError {
                error: "missing_token".to_string(),
                message: "Authorization header required".to_string(),
            })?;

        // Extract bearer token
        let token = auth_header
            .strip_prefix("Bearer ")
            .ok_or_else(|| AuthError {
                error: "invalid_token".to_string(),
                message: "Invalid Authorization header format".to_string(),
            })?;

        // Get the Casdoor auth verifier from extensions (set by middleware)
        let casdoor_auth = parts
            .extensions
            .get::<CasdoorAuthExt>()
            .ok_or_else(|| AuthError {
                error: "server_error".to_string(),
                message: "Auth provider not configured".to_string(),
            })?;

        // Verify token
        let (uid, name, email) = casdoor_auth.0.verify_token(token).await?;

        Ok(AuthUser { uid, name, email })
    }
}

/// Create a layer that adds the Casdoor auth verifier to request extensions
pub fn casdoor_auth_extension(auth: Arc<CasdoorAuth>) -> axum::Extension<CasdoorAuthExt> {
    axum::Extension(CasdoorAuthExt(auth))
}

impl From<PaywalledAuthUser> for AuthUser {
    fn from(p: PaywalledAuthUser) -> Self {
        AuthUser {
            uid: p.uid,
            name: p.name,
            email: p.email,
        }
    }
}

/// Authenticated user extractor that ALSO enforces:
/// 1. BYOK fingerprint validation (SHA-256 against Firestore enrollment)
/// 2. Desktop trial paywall (plan + BYOK + account age)
///
/// If the user is BYOK-active but sends mismatched fingerprints → HTTP 403.
/// If the user is past their trial → HTTP 402.
///
/// `byok_stripped`: true if the request carried BYOK headers that were silently
/// cleared (non-enrolled user or expired heartbeat). Route handlers should check
/// this flag and ignore BYOK headers when true.
///
/// Use this for every $-incurring route handler in the Rust backend:
/// proxy.rs (Gemini), chat_completions.rs (Anthropic), screen_activity.rs
/// (Pinecone), tts.rs, agent.rs.
#[derive(Debug, Clone)]
pub struct PaywalledAuthUser {
    pub uid: String,
    pub name: Option<String>,
    pub email: Option<String>,
    pub byok_stripped: bool,
}

#[async_trait]
impl<S> FromRequestParts<S> for PaywalledAuthUser
where
    S: Send + Sync,
{
    type Rejection = AuthError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        // Get + extract bearer token
        let auth_header = parts
            .headers
            .get("Authorization")
            .and_then(|h| h.to_str().ok())
            .ok_or_else(|| AuthError {
                error: "missing_token".to_string(),
                message: "Authorization header required".to_string(),
            })?;

        let token = auth_header
            .strip_prefix("Bearer ")
            .ok_or_else(|| AuthError {
                error: "invalid_token".to_string(),
                message: "Invalid Authorization header format".to_string(),
            })?;

        // Verify the Casdoor token (same flow AuthUser uses)
        let casdoor_auth = parts
            .extensions
            .get::<CasdoorAuthExt>()
            .ok_or_else(|| AuthError {
                error: "server_error".to_string(),
                message: "Auth provider not configured".to_string(),
            })?;

        let (uid, name, email) = casdoor_auth.0.verify_token(token).await?;

        // BYOK fingerprint validation (issue #7357).
        // Validates SHA-256 fingerprints against Firestore enrollment.
        // Non-BYOK users who send BYOK headers get them silently cleared.
        let mut byok_stripped = false;
        if let Some(byok_ext) = parts.extensions.get::<crate::byok::ByokCacheExt>() {
            // Get the Firestore service from the paywall checker (shares the same Arc)
            if let Some(checker) = parts.extensions.get::<crate::paywall::PaywallCheckerExt>() {
                let byok_state = byok_ext
                    .0
                    .get_or_fetch(&uid, &checker.0.firestore)
                    .await;

                match crate::byok::validate_byok_request(&uid, &parts.headers, &byok_state) {
                    Ok(crate::byok::ByokValidation::Active) => {
                        // BYOK keys validated, proceed with user's keys
                    }
                    Ok(crate::byok::ByokValidation::Inactive { clear_headers }) => {
                        byok_stripped = clear_headers;
                    }
                    Err(error_msg) => {
                        tracing::warn!(
                            "BYOK validation failed for uid={}: {}",
                            uid,
                            error_msg
                        );
                        return Err(AuthError {
                            error: "byok_validation_failed".to_string(),
                            message: error_msg,
                        });
                    }
                }
            }
        }

        // Paywall check — fail open if Firestore is unreachable so a backend
        // outage never makes paying users look paywalled.
        if let Some(checker) = parts.extensions.get::<crate::paywall::PaywallCheckerExt>() {
            if checker.0.is_paywalled(&uid, &parts.headers, byok_stripped).await {
                return Err(AuthError {
                    error: "trial_expired".to_string(),
                    message: "Desktop trial expired. Upgrade or bring your own keys.".to_string(),
                });
            }
        } else {
            tracing::warn!(
                "PaywalledAuthUser: PaywallChecker extension missing, failing open for uid={}",
                uid
            );
        }

        Ok(PaywalledAuthUser {
            uid,
            name,
            email,
            byok_stripped,
        })
    }
}

/// Layer that adds the paywall checker to request extensions.
pub fn paywall_checker_extension(
    checker: Arc<crate::paywall::PaywallChecker>,
) -> axum::Extension<crate::paywall::PaywallCheckerExt> {
    axum::Extension(crate::paywall::PaywallCheckerExt(checker))
}

/// Layer that adds the BYOK state cache to request extensions.
pub fn byok_cache_extension(
    cache: Arc<crate::byok::ByokStateCache>,
) -> axum::Extension<crate::byok::ByokCacheExt> {
    axum::Extension(crate::byok::ByokCacheExt(cache))
}

#[cfg(test)]
mod tests {
    use super::*;
    use jsonwebtoken::{encode, EncodingKey, Header};
    use rsa::pkcs1::EncodeRsaPrivateKey;
    use rsa::pkcs8::EncodePublicKey;
    use rsa::{RsaPrivateKey, RsaPublicKey};
    use serde::Serialize;

    const KID: &str = "test-kid";
    const AUD: &str = "test-client-id";

    #[derive(Serialize)]
    struct TestClaims {
        sub: String,
        aud: String,
        exp: u64,
        iat: u64,
        name: Option<String>,
        email: Option<String>,
        preferred_username: Option<String>,
    }

    fn now() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs()
    }

    /// A verifier preloaded with one RSA key, plus the matching EncodingKey used
    /// to mint test tokens. Keys are generated fresh per test — no committed key material.
    async fn setup() -> (CasdoorAuth, EncodingKey) {
        let mut rng = rand::thread_rng();
        let priv_key = RsaPrivateKey::new(&mut rng, 2048).expect("rsa keygen");
        let pub_pem = RsaPublicKey::from(&priv_key)
            .to_public_key_pem(rsa::pkcs8::LineEnding::LF)
            .unwrap();
        let priv_pem = priv_key.to_pkcs1_pem(rsa::pkcs1::LineEnding::LF).unwrap();

        let decoding = DecodingKey::from_rsa_pem(pub_pem.as_bytes()).unwrap();
        let encoding = EncodingKey::from_rsa_pem(priv_pem.as_bytes()).unwrap();

        let auth = CasdoorAuth::new("https://casdoor.example".into(), None, AUD.into());
        auth.keys.write().await.insert(KID.to_string(), decoding);
        (auth, encoding)
    }

    fn sign(enc: &EncodingKey, kid: &str, claims: &TestClaims) -> String {
        let mut header = Header::new(jsonwebtoken::Algorithm::RS256);
        header.kid = Some(kid.to_string());
        encode(&header, claims, enc).unwrap()
    }

    fn claims(sub: &str, aud: &str, exp: u64) -> TestClaims {
        TestClaims {
            sub: sub.into(),
            aud: aud.into(),
            exp,
            iat: now(),
            name: None,
            email: None,
            preferred_username: None,
        }
    }

    #[tokio::test]
    async fn accepts_valid_token_and_extracts_claims() {
        let (auth, enc) = setup().await;
        let mut c = claims("omi/alice", AUD, now() + 3600);
        c.name = Some("Alice".into());
        c.email = Some("alice@omi.me".into());
        let token = sign(&enc, KID, &c);

        let (uid, name, email) = auth.verify_token(&token).await.expect("valid token");
        assert_eq!(uid, "omi/alice"); // uid == `sub`, matches Python dependencies.py
        assert_eq!(name.as_deref(), Some("Alice"));
        assert_eq!(email.as_deref(), Some("alice@omi.me"));
    }

    #[tokio::test]
    async fn name_falls_back_to_preferred_username() {
        let (auth, enc) = setup().await;
        let mut c = claims("omi/bob", AUD, now() + 3600);
        c.preferred_username = Some("bob".into());
        let token = sign(&enc, KID, &c);

        let (uid, name, _) = auth.verify_token(&token).await.unwrap();
        assert_eq!(uid, "omi/bob");
        assert_eq!(name.as_deref(), Some("bob"));
    }

    #[tokio::test]
    async fn rejects_wrong_audience() {
        let (auth, enc) = setup().await;
        let token = sign(&enc, KID, &claims("omi/eve", "some-other-client", now() + 3600));
        assert!(auth.verify_token(&token).await.is_err());
    }

    #[tokio::test]
    async fn rejects_expired_token() {
        let (auth, enc) = setup().await;
        // Well past the default 60s leeway.
        let token = sign(&enc, KID, &claims("omi/eve", AUD, now() - 3600));
        assert!(auth.verify_token(&token).await.is_err());
    }

    #[tokio::test]
    async fn rejects_unknown_kid() {
        let (auth, enc) = setup().await;
        let token = sign(&enc, "other-kid", &claims("omi/x", AUD, now() + 3600));
        let err = auth.verify_token(&token).await.unwrap_err();
        assert!(err.message.contains("Unknown key id"), "got: {}", err.message);
    }

    #[tokio::test]
    async fn rejects_token_signed_by_a_different_key() {
        let (auth, _enc) = setup().await;
        // A second, unrelated keypair signs a token with the SAME kid the verifier knows.
        let mut rng = rand::thread_rng();
        let attacker = RsaPrivateKey::new(&mut rng, 2048).unwrap();
        let attacker_pem = attacker.to_pkcs1_pem(rsa::pkcs1::LineEnding::LF).unwrap();
        let attacker_enc = EncodingKey::from_rsa_pem(attacker_pem.as_bytes()).unwrap();

        let token = sign(&attacker_enc, KID, &claims("omi/eve", AUD, now() + 3600));
        assert!(auth.verify_token(&token).await.is_err());
    }

    #[tokio::test]
    async fn rejects_malformed_token() {
        let (auth, _) = setup().await;
        assert!(auth.verify_token("not.a.jwt").await.is_err());
    }

    #[test]
    fn jwks_url_prefers_internal_then_public_and_trims_slashes() {
        let a = CasdoorAuth::new(
            "https://door.example/".into(),
            Some("http://casdoor:8000/".into()),
            "cid".into(),
        );
        assert_eq!(
            a.jwks_urls,
            vec![
                "http://casdoor:8000/.well-known/jwks".to_string(),
                "https://door.example/.well-known/jwks".to_string(),
            ]
        );

        let b = CasdoorAuth::new("https://door.example".into(), None, "cid".into());
        assert_eq!(b.jwks_urls, vec!["https://door.example/.well-known/jwks".to_string()]);

        // Empty internal URL is ignored.
        let c = CasdoorAuth::new("https://door.example".into(), Some("".into()), "cid".into());
        assert_eq!(c.jwks_urls, vec!["https://door.example/.well-known/jwks".to_string()]);
    }
}
