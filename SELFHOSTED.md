# Self-hosted Omi [fork-only]

How to deploy the backend, build the Mac app, ship the iOS app and sync with
upstream.

This fork runs upstream Omi on Casdoor (sign-in), MongoDB (data), MinIO (files)
and a local LLM. The design is in the `[fork]` section of `CLAUDE.md`.

Run `make` in the repo root to list the commands.

## Keep secrets out of git

Secrets live in only these places:

| Secret | Where |
|---|---|
| Backend keys | Kubernetes Secret `dev-omi-backend-secrets` (namespace `omi`) |
| Mac app settings | `desktop/macos/.env.app` (gitignored) |
| iOS settings, App Store Connect key path | `app/.env.selfhosted` (gitignored) |
| App Store Connect key | the `.p8` file, outside the repo |

Do not use `git add -A` in this repo. The tree holds old untracked files that
must stay out of git, for example `registry.key`. Add each file by its path.

## Deploy the backend

### What runs

Everything is in namespace `omi` on the microk8s cluster. The image registry is
`192.168.1.42:32000`.

| Service | Deployed by | Address |
|---|---|---|
| `omi-backend` (API) | helm release `omi-backend`, `make deploy` | `omi.spangled-kettle.ts.net`, and plain http at `omi-auth.spangled-kettle.ts.net` for iOS sign-in |
| `pusher` | `kubectl set image`, `make deploy` | inside the cluster |
| `desktop-backend-py` | image: `make deploy`. Settings: helm release `desktop-backend-py` | `desktop-backend-py.spangled-kettle.ts.net` |
| `omi-minio` (files) | helm release `omi`, `make deploy` | `omi-files.spangled-kettle.ts.net` |
| `diarizer` (speakers) | `make deploy-diarizer` | inside the cluster |
| `typesense` (search, vectors) | helm release `omi-typesense` | inside the cluster |
| `omi-backend-memory-maintenance` | cron job in the `omi-backend` chart | runs each hour |

MongoDB, Redis and Casdoor run in namespace `root`. The LLM runs on the GPU
machine. Its address is in the ConfigMap `omi-backend-config`.

### Normal deploy

Commit your work and merge it to `main`. Then, from the repo root:

```bash
make deploy          # build, push and deploy the current commit
make deploy-status   # show the image that each server runs
```

The image tag is the commit. The build reads `git archive <commit>`, so local
files never go into an image. The deploy refuses uncommitted changes in
`backend/` and commits that are not on `main`.

### Small code change

An overlay adds changed files to the running images. It is faster than a full
build.

```bash
make deploy-overlay REF=<commit> DRY_RUN=1        # preview
make deploy-overlay REF=<commit>                  # all 3 servers
make deploy-overlay REF=<commit> ONLY="pusher"    # one server
```

An overlay refuses changes to dependencies, Dockerfiles or charts, and deleted
or renamed files. Use `make deploy` for those.

### Other parts

```bash
make deploy-diarizer

cd backend
helm upgrade desktop-backend-py charts/desktop-backend -n omi \
  -f charts/desktop-backend/local_omi_desktop_backend_values.yaml \
  --set image.tag=<tag from make deploy-status>
helm upgrade --install omi-typesense charts/typesense -n omi \
  -f charts/typesense/local_omi_typesense_values.yaml
```

`make deploy` does not apply chart changes for `desktop-backend-py`. Run the
helm command above when you change its values file. Always give `image.tag`: the
values file has the tag `local`, and that image does not exist in the registry.

### Settings

| Setting | Where | How to change |
|---|---|---|
| Plain settings for `omi-backend` | `backend/charts/backend-listen/local_omi_backend_listen_values.yaml` | edit, commit, `make deploy` |
| Secrets | Secret `dev-omi-backend-secrets` | `kubectl -n omi patch secret dev-omi-backend-secrets --type merge -p '{"stringData":{"KEY":"VALUE"}}'`, then restart the pods |
| Shared addresses | ConfigMap `omi-backend-config` | `kubectl -n omi edit cm omi-backend-config`, then restart the pods |
| LLM model, voice mode | Mongo, through Settings in the Mac app | takes effect in about 30 seconds |

These settings must stay as they are:

- `SELF_HOSTED=true` on every server. It removes the paywall and quota limits.
- `OMI_ENV_STAGE=local`. With `prod`, the server wants Cloud Tasks and does not
  start.
- Pods that use the LLM need the label `access-gpu: "true"`. A network policy
  blocks the others.

### Things that are not in the repo

A rebuild from an empty cluster needs these items too. The repo has no files for
them, so copy them from the live cluster first.

- The Secret `dev-omi-backend-secrets` and the ConfigMap `omi-backend-config`.
- The `pusher` Deployment and its environment. The `make deploy` command changes
  only its image. The chart `backend/charts/pusher` has no values for this cluster.
- The `ollama-embed` Deployment (local embeddings).
- MongoDB, Redis, Casdoor and the LLM machine.

The Casdoor application must allow `<BASE_API_URL>/v1/auth/callback` as a
redirect address.

### When a deploy fails

| Symptom | Cause | Fix |
|---|---|---|
| Image pulls time out | The registry (zot) has stuck requests. Look for `waiting on channel` in its log. | `kubectl -n container-registry delete pod zot-0` |
| `http: server gave HTTP response to HTTPS client` | A microk8s restart removed the node's registry setting. | Add `/var/snap/microk8s/current/args/certs.d/192.168.1.42:32000/hosts.toml` with `server="http://192.168.1.42:32000"` and `capabilities=["pull","resolve"]`, then run `sudo snap restart microk8s.daemon-containerd` |
| Helm says `valueFrom may not be specified when value is not empty` | Someone set a value by hand on the live Deployment. | `kubectl -n omi set env deploy/omi-backend NAME-`, then deploy again |
| New pod stays `Pending` | The node has no space for a second pod. | Keep the `Recreate` strategy in the values file |

## Build the Mac app

You need a Mac with Xcode.

**1. Make `desktop/macos/.env.app`** (only once):

```bash
OMI_PYTHON_API_URL=https://omi.spangled-kettle.ts.net/
OMI_AUTH_API_URL=https://omi.spangled-kettle.ts.net/
OMI_DESKTOP_API_URL=https://desktop-backend-py.spangled-kettle.ts.net/
OMI_FIREBASE_REST_BASE_URL=https://omi.spangled-kettle.ts.net/
FIREBASE_API_KEY=placeholder
```

`FIREBASE_API_KEY` must not be empty, but the server ignores its value. Without
`OMI_FIREBASE_REST_BASE_URL`, the app signs in to Omi's cloud, not to Casdoor.

**2. Build and open the app:**

```bash
make -C desktop run-selfhosted               # normal build
make -C desktop run-selfhosted FULL=1        # full rebuild
make -C desktop run-selfhosted APP_NAME=omi-test   # a second copy for tests
```

This puts the app at `/Applications/omi-selfhosted.app`, with the bundle ID
`com.omi.omi-selfhosted`. It does not start a local backend or a tunnel. It does
not touch the production Omi app.

**3. Sign in** with "Continue with Casdoor".

To change settings after the build, open Settings > AI & Automation > Self-hosted
Server. This page holds the server addresses, the LLM model and the voice mode.

If a quiet microphone gives no transcript, send the audio to the server:

```bash
defaults write com.omi.omi-selfhosted forceCloudSTT -bool true
```

## Ship the iOS app

The fork has an iOS build only. There is no Android build for the self-hosted
stack.

**1. Set up the Mac** (only once):

- Install Flutter 3.44.5 at `~/fvm/versions/3.44.5`.
- Install CocoaPods and fastlane.
- Sign in to your Apple account in Xcode.
- Run `xcodebuild -downloadPlatform iOS`.

**2. Make `app/.env.selfhosted`** (only once):

```bash
OMI_API_BASE_URL=https://omi.spangled-kettle.ts.net/
OMI_AUTH_HTTP_HOST=omi-auth.spangled-kettle.ts.net
IOS_BUNDLE_ID=com.omi-chakra-ios
IOS_TEAM_ID=<Apple team ID>
IOS_DISPLAY_NAME=omi-chakra
FLUTTER=~/fvm/versions/3.44.5/bin/flutter
APP_STORE_CONNECT_API_KEY_KEY_ID=<key ID>
APP_STORE_CONNECT_API_KEY_ISSUER_ID=<issuer ID>
APP_STORE_CONNECT_API_KEY_KEY_FILEPATH=~/path/to/AuthKey_XXXX.p8
```

**3. Build:**

```bash
make mobile-ios-run ARGS="-d <device-id>"   # install on a connected iPhone
make mobile-ios                             # build an .ipa only
make mobile-ios-upload                      # next build number, build, TestFlight
```

The build shows in TestFlight about 10 minutes after the upload.

To upload the last `.ipa` again, or to check the bundle IDs in App Store Connect:

```bash
cd app
fastlane ios upload
fastlane ios check
fastlane ios register   # only for a new bundle ID
```

If the upload or the build fails:

- If all App Store Connect calls fail, the account holder must accept a new Apple
  agreement.
- If `pod install` shows "HTTP2 framing" errors, run the command again.
- The backend must have its plain http sign-in address (`omi-auth`). The iOS
  sign-in does not work without it.

## Sync with upstream

Upstream (`BasedHardware/omi`) adds about 250 commits each day. A sync each
week or two keeps the conflicts small. A month gives about 10 conflicts.

### Remotes

| Remote | Repository | Use |
|---|---|---|
| `origin` | `BasedHardware/omi` | Upstream. Fetch only. Never push. |
| `stufork` | `thestumonkey/omi` | The fork. Local `main` tracks `stufork/main`. |
| `ushomi` | `Ushadow-io/omi` | The Ushadow copy. |

The repo push hook runs upstream's PR checks against `origin`. These checks
fail on fork code. The local hook `.git/hooks/pre-push` skips them for pushes to
`stufork`. `make setup` can overwrite that hook. If a push to `stufork` runs the
checks again, add this line after `if [ "$HOOK_NAME" = "pre-push" ]; then`:

```bash
  if [ "${1:-}" = "stufork" ]; then echo "pre-push: push to stufork, upstream checks skipped" >&2; exit 0; fi
```

### Steps

**1. Merge in a separate worktree.** Then `main` does not change until the sync
works.

```bash
git fetch origin main
UP=$(git rev-parse --short origin/main)     # keep this commit as the target
git worktree add -b sync/upstream-$(date +%F) .claude/worktrees/upstream-sync main
cd .claude/worktrees/upstream-sync
git merge --no-ff --no-commit $UP
```

Use `$UP`, not `origin/main`, for the rest of the sync. A later fetch from any
worktree moves `origin/main`.

**2. Fix each conflict with these rules:**

- Fork code lives in fork-only files, or in a block at the end of an upstream
  file. Such a block replaces an upstream function by name, under an
  environment check such as `SELF_HOSTED`.
- When upstream adds code at the end of a file, put upstream's code first and
  the fork block after it.
- When upstream removes or renames a function that a fork block replaces, find
  the new name and change the fork block. Do not bring the old upstream code
  back.
- When upstream moves code to a new file, move the fork change to the end of
  that new file. Keep the old upstream file the same as upstream.
- Check that each fork block still matches upstream's function signature.

**3. Check that no upstream files are missing:**

```bash
comm -23 <(git ls-tree -r --name-only $UP | sort) <(git ls-files | sort)
```

The list must be empty, or contain only files that the fork deletes on purpose.

**4. Commit the merge:**

```bash
OMI_SKIP_DART_FORMAT=1 OMI_SKIP_WEB_FORMAT=1 git commit
```

The commit hook formats the staged files, and this can take several minutes. It
refuses to commit firmware C files without `clang-format` 18. If it says so, put
version 18 on your `PATH` (for example with `uv pip install clang-format==18.1.8`
in a scratch venv).
After the commit, upstream files that the fork does not change must be the same
as upstream. This command must show only fork files:

```bash
git diff --name-only $UP HEAD
```

**5. Check the Python packages.** The images install from `backend/pylock*.toml`,
not from `requirements.txt`. Each lock must contain `pymongo` and `boto3`, and
must not contain `firebase-admin`:

```bash
grep -cE 'name = "(pymongo|boto3)"' backend/pylock.runtime.toml backend/pusher/pylock.toml
```

If a lock is wrong, run `backend/scripts/update-python-lock.sh`. It takes about
10 minutes.

**6. Test the backend.** Run these from `backend/`:

```bash
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest -q $(ls tests/unit/*.py | grep -iE 'selfhost|casdoor|mongo|minio|typesense_vector|firebase_admin|wrapped_token')
```

Then start the app with the self-hosted environment (`SELF_HOSTED=true`, fake
`MONGODB_URL`, `S3_*`, `CASDOOR_*` and `SELF_HOSTED_LLM_URL` values are enough).
Use `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` for the Opus library. Make sure
of these points:

- `import main` works, and `GET /v1/health` gives 200.
- `/v1/auth/token` is served by `routers.casdoor_auth`.
- `firebase_admin` comes from `backend/firebase_admin/` (the fork's stand-in).
- The database client is `MongoFirestore`.
- `get_llm('memory_l2')` gives `SelfHostedChatOpenAI`. Upstream changes the LLM
  routing often. If a sync breaks it, every summary and title fails, and no
  test outside the fork tests shows it.

Also import `pusher.main` and `desktop_backend` with the same environment.

Some upstream tests fail because of the fork's design. To find out whether a
failure is new, run the same test file on pure upstream in a second worktree
(`git worktree add --detach <path> $UP`) with the same venv.

**7. Build the apps** when the sync changes their fork files:

```bash
make -C desktop run-selfhosted FULL=1
make mobile-ios
```

**8. Merge to `main`, push to the fork and deploy:**

```bash
cd /Users/stu/repos/omi
git merge --ff-only sync/upstream-<date>
git push stufork main
make deploy
```

A sync changes dependencies and Dockerfiles, so use `make deploy`, not an
overlay.
