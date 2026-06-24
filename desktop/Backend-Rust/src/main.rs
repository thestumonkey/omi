// OMI Desktop Backend - Rust
// Port from Python backend (main.py)

use axum::Router;
use std::fs::OpenOptions;
use std::io::LineWriter;
use std::sync::Arc;
use tower_http::cors::{Any, CorsLayer};
use tower_http::trace::TraceLayer;
use tracing_subscriber::fmt::format::Writer;
use tracing_subscriber::fmt::time::FormatTime;
use tracing_subscriber::{fmt, layer::SubscriberExt, util::SubscriberInitExt};

/// Custom time formatter: [HH:mm:ss] [backend]
#[derive(Clone)]
struct BackendTimer;

impl FormatTime for BackendTimer {
    fn format_time(&self, w: &mut Writer<'_>) -> std::fmt::Result {
        let now = chrono::Utc::now();
        write!(w, "[{}] [backend]", now.format("%H:%M:%S"))
    }
}

mod auth;
mod byok;
mod config;
mod encryption;
mod llm;
mod models;
mod paywall;
mod routes;
mod services;
mod vertex;

use auth::{byok_cache_extension, casdoor_auth_extension, paywall_checker_extension, CasdoorAuth};
use byok::ByokStateCache;
use paywall::PaywallChecker;
use config::Config;
use routes::{
    // Active (real traffic from current app)
    agent_routes, auth_routes, chat_completions_routes, config_routes, crisp_routes,
    health_routes, proxy_routes, screen_activity_routes, tts_routes, updates_routes,
    webhook_routes,
    // Deprecated stubs (return 410 Gone — current app uses Python for all data CRUD)
    deprecated_routes,
};
use services::{FirestoreService, IntegrationService, RedisService};

/// Application state shared across handlers
#[derive(Clone)]
pub struct AppState {
    pub firestore: Arc<FirestoreService>,
    pub integrations: Arc<IntegrationService>,
    pub redis: Option<Arc<RedisService>>,
    pub config: Arc<Config>,
    pub crisp_session_cache: routes::crisp::SessionCache,
    pub gemini_rate_limiter: routes::rate_limit::SharedRateLimiter,
    /// Vertex AI auth provider (present when USE_VERTEX_AI=true)
    pub vertex_auth: Option<vertex::VertexAuth>,
}

#[tokio::main]
async fn main() {
    // Open log file (same as Swift dev app: /tmp/omi-dev.log)
    // Wrap in LineWriter to flush after each line (ensures logs appear immediately)
    let log_file = OpenOptions::new()
        .create(true)
        .append(true)
        .open("/tmp/omi-dev.log")
        .expect("Failed to open log file");
    let line_writer = LineWriter::new(log_file);

    // Use non_blocking for proper async file writing
    let (non_blocking, _guard) = tracing_appender::non_blocking(line_writer);

    // Initialize tracing with both stdout and file output
    // Format: [HH:mm:ss] [backend] message
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "omi_desktop_backend=info,tower_http=info".into()),
        )
        // Stdout layer
        .with(
            fmt::layer()
                .with_timer(BackendTimer)
                .with_target(false)
                .with_level(false)
                .with_ansi(true)
        )
        // File layer (same format, no ANSI colors)
        .with(
            fmt::layer()
                .with_timer(BackendTimer)
                .with_target(false)
                .with_level(false)
                .with_ansi(false)
                .with_writer(non_blocking)
        )
        .init();

    // Load environment variables
    dotenvy::dotenv().ok();

    // Log active QoS tier
    tracing::info!("Model QoS tier: {} | rate limits: soft={}, hard={}",
        llm::model_qos::tier_description(),
        llm::model_qos::daily_soft_limit(),
        llm::model_qos::daily_hard_limit(),
    );

    // Load and validate config
    let config = Config::from_env();
    if let Err(e) = config.validate() {
        tracing::error!("Configuration error: {}", e);
    }

    // Initialize Firebase Auth
    // Casdoor OIDC verifier — validates the id_tokens the desktop app sends as
    // Bearer tokens (issued by the Python backend's Casdoor flow). audience =
    // CASDOOR_CLIENT_ID; JWKS fetched from {CASDOOR_ENDPOINT}/.well-known/jwks.
    let casdoor_endpoint = config.casdoor_endpoint.clone()
        .expect("CASDOOR_ENDPOINT must be set");
    let casdoor_client_id = config.casdoor_client_id.clone()
        .expect("CASDOOR_CLIENT_ID must be set");
    let casdoor_auth = Arc::new(CasdoorAuth::new(
        casdoor_endpoint,
        config.casdoor_internal_url.clone(),
        casdoor_client_id,
    ));

    // Refresh JWKS with retry (transient network failures at startup)
    {
        let max_attempts = 3u32;
        let mut last_err = None;
        for attempt in 1..=max_attempts {
            match casdoor_auth.refresh_keys().await {
                Ok(_) => {
                    if attempt > 1 {
                        tracing::info!("Casdoor JWKS fetched on attempt {}", attempt);
                    }
                    last_err = None;
                    break;
                }
                Err(e) => {
                    tracing::warn!("Casdoor JWKS fetch attempt {}/{} failed: {}", attempt, max_attempts, e);
                    last_err = Some(e);
                    if attempt < max_attempts {
                        tokio::time::sleep(std::time::Duration::from_secs(1 << (attempt - 1))).await;
                    }
                }
            }
        }
        if let Some(e) = last_err {
            tracing::warn!("All {} Casdoor JWKS fetch attempts failed: {} - auth may not work", max_attempts, e);
        }
    }

    // Initialize Firestore
    let firestore_project_id = config.firebase_project_id.clone()
        .expect("FIREBASE_PROJECT_ID must be set for Firestore");
    let firestore = match FirestoreService::new(
        firestore_project_id.clone(),
        config.encryption_secret.clone(),
    ).await {
        Ok(fs) => Arc::new(fs),
        Err(e) => {
            tracing::warn!("Failed to initialize Firestore: {} - using placeholder", e);
            Arc::new(FirestoreService::new(firestore_project_id, config.encryption_secret.clone()).await.unwrap())
        }
    };

    // Initialize Integration Service
    let integrations = Arc::new(IntegrationService::new());

    // Initialize Redis (optional - for conversation visibility/sharing)
    // Use explicit connection params to avoid URL encoding issues with special characters in password
    let redis = if let Some(host) = &config.redis_host {
        match RedisService::new_with_params(host, config.redis_port, config.redis_password.as_deref()) {
            Ok(rs) => {
                tracing::info!("Redis client created for {}:{}", host, config.redis_port);
                Some(Arc::new(rs))
            }
            Err(e) => {
                tracing::warn!("Failed to create Redis client: {} - conversation sharing will not work", e);
                None
            }
        }
    } else {
        tracing::warn!("Redis not configured - conversation sharing will not work");
        None
    };

    // Initialize Vertex AI auth (when USE_VERTEX_AI=true)
    let vertex_auth = if config.use_vertex_ai {
        match (config.vertex_project_id.as_ref(), &config.vertex_location) {
            (Some(project_id), location) => {
                match vertex::VertexAuth::new(project_id.clone(), location.clone()).await {
                    Ok(auth) => {
                        // Verify we can get a token at startup
                        match auth.token().await {
                            Ok(_) => {
                                tracing::info!("Vertex AI auth initialized (project={}, location={})", project_id, location);
                                Some(auth)
                            }
                            Err(e) => {
                                tracing::error!("Vertex AI token fetch failed: {} — falling back to API key", e);
                                None
                            }
                        }
                    }
                    Err(e) => {
                        tracing::error!("Vertex AI init failed: {} — falling back to API key", e);
                        None
                    }
                }
            }
            _ => {
                tracing::warn!("USE_VERTEX_AI=true but no project ID — falling back to API key");
                None
            }
        }
    } else {
        None
    };

    // Create app state
    let gemini_rate_limiter = routes::rate_limit::GeminiRateLimiter::new();

    // Spawn background task to evict stale rate limit entries every hour
    {
        let limiter = gemini_rate_limiter.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(std::time::Duration::from_secs(3600)).await;
                limiter.evict_stale().await;
            }
        });
    }

    // BYOK state cache — caches Firestore BYOK state per-uid for 30s.
    let byok_cache = Arc::new(ByokStateCache::new());

    // Paywall checker — reads subscription/BYOK/account-age from Firestore + Firebase Auth.
    // NOTE: still Firebase-coupled (Phase 2 migrates Firestore → Mongo); the project
    // id is only used for the Firestore/Identity-Toolkit calls inside the checker.
    let firebase_project_id = config.firebase_auth_project_id.clone()
        .or_else(|| config.firebase_project_id.clone())
        .expect("FIREBASE_AUTH_PROJECT_ID or FIREBASE_PROJECT_ID must be set (Firestore, pending Phase 2)");
    let paywall_checker = Arc::new(PaywallChecker::new(
        firestore.clone(),
        firebase_project_id,
        byok_cache.clone(),
        config.self_hosted,
    ));

    let state = AppState {
        firestore,
        integrations,
        redis,
        config: Arc::new(config.clone()),
        crisp_session_cache: routes::crisp::new_session_cache(),
        gemini_rate_limiter,
        vertex_auth,
    };

    // Build CORS layer
    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    // Build auth router (has its own state)
    let auth_router = auth_routes(state.config.clone());

    // Build main app router with AppState
    let main_router = Router::new()
        // ── Active routes (real traffic from current desktop app) ──────────
        .merge(health_routes())
        .merge(agent_routes())
        .merge(config_routes())
        .merge(crisp_routes())
        .merge(proxy_routes())
        .merge(screen_activity_routes())
        .merge(tts_routes())
        .merge(chat_completions_routes())
        .merge(updates_routes())
        .merge(webhook_routes())
        // ── Deprecated stubs (return 410 Gone) ───────────────────────────
        // Current app uses Python (api.omi.me) for all data CRUD.
        .merge(deprecated_routes())
        .with_state(state);

    // Merge both (now both are Router<()>), then add layers
    let app = main_router
        .merge(auth_router)
        .layer(casdoor_auth_extension(casdoor_auth))
        .layer(paywall_checker_extension(paywall_checker))
        .layer(byok_cache_extension(byok_cache))
        .layer(cors)
        .layer(TraceLayer::new_for_http());

    // Start server
    let addr = format!("0.0.0.0:{}", config.port);
    tracing::info!("Starting OMI Desktop Backend on {}", addr);

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
