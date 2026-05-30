# Harvest Audit — fork → fresh-from-upstream + shim

**Strategy:** branch from upstream HEAD (done — this branch), keep the Firestore→Mongo
shim (committed), then re-apply ("harvest") only the genuinely-ours seams from the old
fork. The native DB rewrite is **discarded** because the shim makes upstream `database/*.py`
run verbatim.

Baseline: fork `main` delta vs merge-base `88d2ede0` = **464 files**. Classification below.

| Bucket | Files | Verdict | Method |
|---|---|---|---|
| Native DB rewrite (upstream has counterpart) | ~23 | **DISCARD** | Shim runs upstream verbatim. Drop `backend/database/**` from `.gitattributes`. Old rewrite = test oracle, then delete. |
| DB infra, fork-only (`_indexes.py`, `audio_chunks.py`) | 2 | **PORT** | Already speak the Firestore API → run on shim unchanged. |
| Casdoor auth + config (backend) | 32 | **PORT** | Semantic replacement of Firebase auth — cherry-pick. `dependencies.py`, `utils/oidc.py`, `routers/auth.py`, `routers/custom_auth.py`, `routers/oauth.py`, `database/auth.py`, `config/casdoor/**`, `compose/casdoor.yml`. |
| FCM stub / notifications | 2 | **PORT** | `utils/fcm_stub.py` (fork-only) + `utils/notifications.py` wiring. |
| Flutter auth (Casdoor) | 4 | **PORT** | `services/auth_service.dart`, `providers/auth_provider.dart`, `pages/onboarding/auth.dart`, `backend/auth.dart`. |
| Storage GCS → MinIO/S3/GridFS | 0 (new) | **BUILD FRESH** | `utils/other/storage.py` — the ONLY load-bearing Google dep left in running pods. Not yet done in fork. |
| Rust desktop backend | 18 | **MOSTLY DISCARD** | Follow upstream: it routes desktop data CRUD → Python backend (our Mongo). Keep only a thin `mongo`/config seam if still needed; narrow `.gitattributes` from `src/**` to one file. |
| omisend plugins/examples | 124 | **COPY WHOLESALE** | Fork-only (0 in upstream) → copy the dir, never conflicts. Confirm still wanted. |
| App feature changes (non-auth) | ~42 | **REVIEW** | Fork features: LLM/STT settings (`models/llm_preset.dart`, `models/stt_provider.dart`, `pages/settings/llm_settings_page.dart`, `transcription_settings_page.dart`), persona, action-items UI, etc. Port the wanted ones. |
| Desktop app changes (non-auth) | ~29 | **REVIEW** | Swift: `LlmConfig.swift`, settings pages, API base config. Port the wanted ones. |
| Infra / scripts / CI | 24 | **PORT/REVIEW** | `scripts/casdoor_*.py`, `scripts/sync-upstream.sh`+`post-merge-patch.sh`+`setup-merge-drivers.sh` (revise for shim era), `.github/workflows/*_auto_dev.yml`, `docker-compose.yml` (mongo), chart `MONGODB_*`/`CASDOOR_*` env, `.cursor/**`. |
| Noise | 142 | **IGNORE** | `*.lock`, `*.orig`, images/logos, `*.gen.dart`, `legacy/**`. |

## The bottom line

Of 464 changed files, the genuinely-ours migration seams that need **careful porting** are
only **~40**: Casdoor auth (32) + FCM (2) + Flutter auth (4) + DB infra (2) + storage-fresh (1).
The rest is discarded (shim replaces ~23), copied mechanically (omisend 124), feature review
(~70), or noise (142).

## `.gitattributes` rewrite (the rule: `merge=ours` ONLY for files with NO upstream counterpart)

- **REMOVE** `backend/database/** merge=ours` → tracks upstream (shim).
- **REMOVE** `desktop/Backend-Rust/src/** merge=ours` → narrow to the one storage seam file, if any.
- **KEEP** only fork-only files: `mongo_firestore.py`, `utils/fcm_stub.py`, `config/casdoor/**`,
  `database/_indexes.py`, `database/audio_chunks.py`, Casdoor Flutter/auth files.

## Required code change beyond the seam (1 line)

`database/users.py` — the only `.transaction()` site — repoint:
`from google.cloud.firestore_v1 import transactional` → `from database.mongo_firestore import transactional`.

## Suggested apply order

1. Shim ✅ (committed) + drop `database/**` from `.gitattributes`.
2. Port Casdoor auth (backend) → run backend boot + auth flow.
3. Port FCM stub + the `users.py` transactional 1-liner.
4. Build storage GCS→MinIO (unblocks removing `GOOGLE_APPLICATION_CREDENTIALS` from charts).
5. Port Flutter auth.
6. Copy omisend; port wanted app/desktop features.
7. Rust: follow upstream (route to Python), narrow `.gitattributes`.
8. Stand up weekly automated upstream-sync CI (PR-on-green) — the anti-drift habit.
