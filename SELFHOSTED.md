# Self-hosted Omi [fork-only]

How to deploy the backend, build the Mac app and ship the iOS app.

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
