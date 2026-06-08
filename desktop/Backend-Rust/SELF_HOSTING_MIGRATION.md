# Desktop Rust Backend — Self-Hosting Migration Plan

Status: **plan only, no code written.** Goal: migrate `omi-desktop-backend`
(`desktop/Backend-Rust`) off Google Cloud (Firebase Auth, Firestore, Vertex AI,
GCE/GCS) onto the same self-hosted stack the Python backend already uses
(Casdoor OIDC + MongoDB + direct LLM APIs), so the desktop app can run fully
self-hosted against `omi.spangled-kettle.ts.net`.

## TL;DR

The `.env` was pre-seeded with Casdoor/Mongo values but **the Rust code never
reads them** — it's still 100% Firebase/Firestore/Vertex (30 files,
`gcp_auth`, Firestore REST). The good news: the **active** GCP surface is far
smaller than it looks, because the live desktop app already does all data CRUD
against the **Python** backend. `main.rs` mounts most data endpoints as
`deprecated_routes()` → **410 Gone**, so ~130 of the 145 functions in the
10k-LOC `services/firestore.rs` are dead code.

## Coupling inventory (by *actual* liveness)

| # | Coupling | Where | Live? | Target |
|---|----------|-------|-------|--------|
| 1 | **Firebase ID-token verify** | `src/auth.rs` (`verify_token`, RS256 vs Firebase JWK, `iss=securetoken.google.com/{proj}`) | ✅ every authed request | Casdoor OIDC — mirror Python `backend/utils/oidc.py` |
| 2 | **Google/Apple OAuth flow** | `src/routes/auth.rs` (authorize/callback/token, `use_custom_token`) | ✅ | Casdoor proxy — mirror Python `backend/routers/auth.py` (already hardened, PR #3) |
| 3 | **Firestore data layer** | `src/services/firestore.rs` (145 fns) | ⚠️ only ~15 fns live | MongoDB (`mongodb` crate) — share collections with Python |
| 4 | **Vertex AI (Gemini)** | `src/vertex.rs`, `src/routes/proxy.rs` (1.8k), `src/routes/chat_completions.rs` | ✅ | **OpenAI** API (drop `gcp_auth`) — needs Gemini→OpenAI shape translation |
| 5 | **GCE agent-VM provisioning + GCS** | `src/routes/agent.rs` | ✅ (agent feature) | **Replace with k8s Jobs/Pods** (own design phase) |
| 6 | Redis | `src/services/redis.rs` | ✅ | Already portable — just point `REDIS_DB_HOST` at self-hosted redis |
| 7 | Pinecone | `src/config.rs` only | ❌ unused | Drop the dead config |

Live Firestore functions (the real Phase-2 surface, ~15 not 145):
`get/set/delete/provision_agent_vm`, `upsert/sync_screen_activity`,
`record_llm_usage`/`get_total_llm_cost`, `create/get_action_items` (webhooks),
plus byok-key storage, paywall/subscription reads, and user lookups in `auth.rs`.

## Strategy: the adapter seam (same trick used in Python)

The Python migration kept call sites stable by swapping implementations behind a
stable interface (`MongoFirestore` shim, `MinioStorageClient`). Do the same here:
keep the `FirestoreService` struct's public method signatures, swap the internals
from Firestore REST to MongoDB. Callers in `routes/*` don't change.

**Interop constraint:** the desktop backend and the Python backend operate on the
**same user data**. Mongo collection names + document shapes MUST match the
Python migration's mapping (see `MEMORY.md` "MongoDB Collection Mapping"):
`conversations`, `memories`, `action_items`, `screen_activity`, etc. Point the
Rust backend at the **same** `MONGODB_URL` the Python backend uses so both read/write
one dataset. Where Python already migrated a collection (e.g. `action_items`), match
it exactly; where Python is still a straggler (`screen_activity` is still Firestore
in Python per MEMORY.md), migrate both together to avoid shape drift.

## Phases (mirror the Python migration order)

### Phase 1 — Auth → Casdoor OIDC  (small, highest value)
- `auth.rs::verify_token`: swap Firebase JWK URL → Casdoor JWKS
  (`door.spangled-kettle.ts.net/.well-known/jwks`), issuer →
  `CASDOOR_ENDPOINT`, keep RS256 (`jsonwebtoken` already does it). `sub` becomes
  Casdoor `org/username` (matches Python uid format).
- `routes/auth.rs`: replace Google/Apple authorize/callback/token with the Casdoor
  proxy flow — port the **already-hardened** Python `routers/auth.py` (redirect_uri
  allowlist + binding + constant-time compare from PR #3).
- Reference: `backend/utils/oidc.py`, `backend/routers/auth.py`.
- Risk: low. Verifiable in isolation (token in → uid out). Desktop app already
  sends Casdoor id_tokens (Phase 3 of the app migration is done).

### Phase 2 — Firestore → MongoDB  (bulk, but only the live ~15 fns matter)
- Add `mongodb` crate; build a Mongo-backed `FirestoreService` keeping the public
  API. Implement the **live** functions first (agent_vm, screen_activity,
  llm_usage, action_items, byok, paywall, user lookup).
- Match Python collection names + shapes; reuse `document_id_from_seed`.
- Delete or stub the ~130 dead functions behind `deprecated_routes()` (they're
  already 410 to clients) — don't port dead code.
- **DECISION: share the Python backend's Mongo** — same `MONGODB_URL` + `MONGODB_DB`
  (`omi`), one dataset. Mirror patterns from MEMORY.md "Key Patterns" (`replace_one`
  upsert, `$set`/`$unset`/`$addToSet`/`$inc`).
- Risk: medium. Test against the shared Mongo; verify Python and Rust agree on shapes
  (especially `action_items`, already on Mongo in Python).

### Phase 3 — Vertex AI → OpenAI  (DECISION: OpenAI, not Gemini)
- **DECISION: consolidate on OpenAI** (`OPENAI_API_KEY`, already in `config.rs`),
  dropping Vertex/Gemini and `gcp_auth` entirely.
- Implication — bigger than a Gemini-AI-Studio swap: the live desktop app calls
  **Gemini-shaped** proxy endpoints (`/v1/proxy/gemini/*`, `/v1/proxy/gemini-stream/*`
  in `proxy.rs`). Moving to OpenAI requires **translating Gemini ↔ OpenAI**
  request/response/stream shapes at the proxy (Gemini `contents`/`parts` ↔ OpenAI
  `messages`; SSE chunk formats differ). `chat_completions.rs` is already
  OpenAI-shaped, so it's a smaller change there.
- Decide whether to keep the `/proxy/gemini/*` route *paths* (translate underneath,
  no app change) or update the desktop app to call OpenAI-shaped routes (cleaner,
  but couples to an app release).
- Risk: medium (translation layer in the 1.8k-LOC `proxy.rs`).

### Phase 4 — Agent-VM feature → k8s Jobs/Pods  (DECISION: replace; own design phase)
- **DECISION: replace GCE VMs + GCS with Kubernetes workloads** on the cluster.
  `routes/agent.rs` currently: provisions a GCE VM per user, tracks status in
  Firestore, stages data in a GCS bucket.
- This is its **own design phase** (largest effort) — sketch before coding:
  - VM lifecycle → a per-user k8s Job/Pod (or StatefulSet) created via the k8s API
    (the backend runs in-cluster, so use a ServiceAccount + the Rust `kube` crate).
  - GCS staging bucket → MinIO (the self-hosted S3 you deployed) bucket.
  - Provisioning status → Mongo (folds into Phase 2's `agent_vm` records).
  - Decide isolation/quotas (namespace per user? resource limits? TTL/cleanup?).
- Risk: high / unknowns. Do Phases 1–3 first; treat this as a follow-on project.

### Phase 5 — Cleanup & deploy
- `config.rs`: drop Firebase/GCP-required vars + dead Pinecone; make Casdoor/Mongo
  required. Remove `gcp_auth` dep and the `google-credentials.json` requirement.
- `desktop/run.sh`: default `OMI_PYTHON_API_URL` + desktop API at the self-hosted
  URLs; the Firebase/creds preflight checks (run.sh ~290–320) can be dropped.
- `Backend-Rust/charts/` + `Dockerfile`: wire the self-hosted env (mirror the
  Python backend charts), deploy to the cluster (namespace `omi`).

## Effort estimate (rough)

| Phase | Effort | Notes |
|---|---|---|
| 1 Auth | S | Direct port of existing Python OIDC + auth router |
| 2 Firestore→Mongo | M | Only ~15 live fns; share Python's Mongo; delete the dead 130 |
| 3 Vertex→OpenAI | M | Gemini↔OpenAI shape translation in `proxy.rs` |
| 4 Agent VMs → k8s | L (own phase) | `kube` crate + MinIO; do after 1–3 |
| 5 Cleanup/deploy | S | Mirror Python charts |

## Decisions (locked)
1. **Phase 4 agent VMs** → **replace with k8s Jobs/Pods** (own design phase, after 1–3).
2. **Mongo** → **share the Python backend's Mongo** (`MONGODB_URL`/`MONGODB_DB=omi`), one dataset.
3. **LLM** → **OpenAI** (`OPENAI_API_KEY`); add a Gemini↔OpenAI translation layer at the proxy.

## Recommended sequencing
Phases **1 → 2 → 3 → 5** deliver a self-hosted desktop backend with the agent-VM
feature temporarily unavailable; then tackle **Phase 4** (k8s agent workloads) as a
follow-on project. Each phase is independently shippable and testable.
