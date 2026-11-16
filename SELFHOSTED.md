# Self-hosted Omi [fork-only]

How to deploy the backend, build the Mac app, ship the iOS app and sync with
upstream.

This fork runs upstream Omi on Casdoor (sign-in), MongoDB (data), MinIO (files)
and a local LLM. The design is in the `[fork]` section of `CLAUDE.md`.

Run `make` in the repo root to list the commands.

The examples use `example.com` for your domain and `registry.example.com:32000`
for your image registry. Put your own values in these files:

| Value | File |
|---|---|
| Image registry | `REGISTRY` in `backend/Makefile` and `backend/scripts/selfhosted-overlay.sh`, and `skaffold.yaml` (or run `make deploy REGISTRY=...`) |
| Server host names | `backend/charts/*/local_*_values.yaml` |
| Casdoor app and redirect address | `backend/config/casdoor/apps.yaml` |

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
`registry.example.com:32000`.

| Service | Deployed by | Address |
|---|---|---|
| `omi-backend` (API) | helm release `omi-backend`, `make deploy` | `omi.example.com`, and plain http at `omi-auth.example.com` for iOS sign-in |
| `pusher` | `kubectl set image`, `make deploy` | inside the cluster |
| `desktop-backend-py` | image: `make deploy`. Settings: helm release `desktop-backend-py` | `desktop-backend-py.example.com` |
| `omi-minio` (files) | helm release `omi`, `make deploy` | `omi-files.example.com` |
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
| `http: server gave HTTP response to HTTPS client` | A microk8s restart removed the node's registry setting. | Add `/var/snap/microk8s/current/args/certs.d/registry.example.com:32000/hosts.toml` with `server="http://registry.example.com:32000"` and `capabilities=["pull","resolve"]`, then run `sudo snap restart microk8s.daemon-containerd` |
| Helm says `valueFrom may not be specified when value is not empty` | Someone set a value by hand on the live Deployment. | `kubectl -n omi set env deploy/omi-backend NAME-`, then deploy again |
| New pod stays `Pending` | The node has no space for a second pod. | Keep the `Recreate` strategy in the values file |

## Build the Mac app

You need a Mac with Xcode.

**1. Make `desktop/macos/.env.app`** (only once):

```bash
OMI_PYTHON_API_URL=https://omi.example.com/
OMI_AUTH_API_URL=https://omi.example.com/
OMI_DESKTOP_API_URL=https://desktop-backend-py.example.com/
OMI_FIREBASE_REST_BASE_URL=https://omi.example.com/
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
OMI_API_BASE_URL=https://omi.example.com/
OMI_AUTH_HTTP_HOST=omi-auth.example.com
IOS_BUNDLE_ID=com.example.omi
IOS_TEAM_ID=<Apple team ID>
IOS_DISPLAY_NAME=omi-selfhosted
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

## Updates and upstream sync

Upstream (`BasedHardware/omi`) adds about 250 commits each day. This fork
follows it with a "hub" model:

- The fork maintainer merges upstream, fixes the fork code, tests it and
  publishes a release tag, for example `selfhosted-2026.11.11`.
- Users update to a release tag. A user who did not change fork code gets no
  conflicts, and needs no LLM.
- A user who wants upstream sooner, or who has their own changes, can run the
  same sync tool, `scripts/fork-sync`.

### Update your instance (users)

Your clone's `origin` is the fork. Do this in the repo root:

```bash
git fetch origin --tags
TAG=$(git tag -l 'selfhosted-*' --sort=-v:refname | head -1)   # the newest release
git merge --ff-only $TAG
make deploy
```

Then check that a new recording gets a title and a summary.

If `git merge --ff-only` fails, you have your own commits. Use the sync tool:

```bash
scripts/fork-sync $TAG
```

The next sections tell how it works.

### Sync with upstream yourself (users and maintainers)

**1. Add upstream as a remote** (one time):

```bash
git remote add upstream https://github.com/BasedHardware/omi.git
```

The tool finds the remote that points at `BasedHardware/omi`, whatever its name.

**2. Pick a fixer.** A fixer fixes merge conflicts and failed checks. Set
`FORK_SYNC_FIXER` in the shell or in `.fork-sync.env` in the repo root. That
file is gitignored.

| Fixer | What it needs | What it fixes |
|---|---|---|
| `claude` | [Claude Code](https://claude.com/claude-code) installed and signed in | conflicts and failed checks. It reads the repo and runs tests. |
| `llm` | any OpenAI-compatible LLM (below) | conflicts only. Each file is sent as text. |
| `none` | nothing | nothing. The tool stops, and you fix it. |

The `claude` fixer can edit files only in the sync worktree. It can run only
read-only git commands and the fork tests. It cannot push, deploy or use the
network. This matters because it reads upstream code, and text in that code
could try to give it orders.

With no setting, the tool uses `claude` if it is installed, then `llm` if an
LLM URL is set, then `none`.

For `llm`, put this in `.fork-sync.env`. You can use the LLM that your Omi
instance uses (`SELF_HOSTED_LLM_URL`):

```bash
FORK_SYNC_LLM_URL=http://your-llm-host:8000/v1
FORK_SYNC_LLM_MODEL=your-model
FORK_SYNC_LLM_KEY=sk-...          # if your LLM needs a key
```

A small local model can fix small conflicts. It often fails on large syncs.
When it fails, the tool stops and lists the files that are left.

**3. Run the sync** from the repo root:

```bash
scripts/fork-sync                 # merge upstream main
scripts/fork-sync <ref>           # or merge a tag or branch
```

The tool does these steps:

1. It makes a new worktree in `.fork-sync/<date>-<commit>`, on a new branch.
   Your current branch does not change.
2. It merges. `git rerere` is on, so git reuses the conflict fixes from the
   last sync.
3. It gives the remaining conflicts to the fixer.
4. It commits the merge.
5. It runs the checks. If they fail, it gives the failures to the fixer (only
   with `claude`), commits the fix and checks again.
6. It prints the next steps. **It never pushes and never deploys.**

The checks are:

- No upstream files are missing.
- The package locks contain `pymongo` and `boto3`, and do not contain
  `firebase-admin`.
- The fork helm values render no GKE-only objects, such as `PodMonitoring`.
  Upstream adds these for its Google cluster. Helm refuses a whole upgrade when
  the cluster does not know a type. This needs `helm`.
- The fork unit tests pass.
- The boot check passes: `backend/scripts/selfhosted_boot_check.py`. It starts
  the three servers with fake addresses. It checks the sign-in routes, the
  database stand-in and the LLM routing. Upstream often breaks these without a
  merge conflict.

The first run takes longer, because it installs the backend packages with `uv`.

**4. If the tool stops**, it tells you why and what to run. Usually you:

```bash
cd .fork-sync/<date>-<commit>
# fix the files that it lists, then:
git add <file>
scripts/fork-sync continue
```

**5. When it passes:**

```bash
git diff <old main>..sync/<branch> --stat    # read the changes
git merge --ff-only sync/<branch>
git push origin main                          # your fork, never upstream
make deploy
git worktree remove .fork-sync/<date>-<commit>
```

A sync changes dependencies and Dockerfiles, so use `make deploy`, not an
overlay.

At the end, the script lists upstream server switches that changed and that
the fork does not match. Upstream sets these in its own servers, not in the
code. One can be needed for the new code to work: with `OMI_SHAPED_AGENT_MODE`
unset, every summary failed. Read each listed switch's code before you deploy.
Set the ones the fork needs in
`backend/charts/backend-listen/local_omi_backend_listen_values.yaml`, and on
`pusher` and `desktop-backend-py` with `kubectl -n omi set env`.

Other commands:

```bash
scripts/fork-sync check       # only the checks, in the current tree (also: make fork-check)
scripts/fork-sync env         # only the switch list: upstream main since our last merge
FORK_SYNC_SKIP_TESTS=1 scripts/fork-sync      # skip the slow backend checks
```

### Publish a release (maintainer)

1. Run `scripts/fork-sync`, merge, push and deploy, as above.
2. Check the real instance: sign in, record, and check for a title and a summary.
   Build the apps if the sync changed their fork files
   (`make -C desktop run-selfhosted FULL=1`, `make mobile-ios`).
3. Tag and push the tag:

```bash
git tag -a selfhosted-$(date +%Y.%m.%d) -m "Self-hosted release on upstream $(git rev-parse --short HEAD^2)"
git push <fork remote> selfhosted-$(date +%Y.%m.%d)
```

Sync each week or two. A month gives about 10 conflicts.

### Pushes to your fork

The repo push hook runs upstream's PR checks against upstream. These checks fail
on fork code. To skip them for every push that does not go to upstream, add this
line to `.git/hooks/pre-push`, after `if [ "$HOOK_NAME" = "pre-push" ]; then`:

```bash
  case "${2:-}" in *BasedHardware/omi*) ;; *) echo "pre-push: not upstream, upstream checks skipped" >&2; exit 0 ;; esac
```

`make setup` can overwrite the hook. Then add the line again.

### Conflict rules

The `claude` fixer reads these rules. Use them when you fix conflicts by hand.

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
- Do not add `firebase-admin` back to a `requirements.txt` or lock. The fork
  replaces it with `backend/firebase_admin/`.
- After the merge, upstream files that the fork does not change must be the same
  as upstream. `git diff --name-only <upstream commit> HEAD` must show only fork
  files.

### Problems that the tool does not fix

- **The commit hook refuses a file.** The hook formats the staged files. The tool
  installs `clang-format` 18 for firmware C files by itself, with `uv`. For any
  other refusal, read the hook message, fix it, then run `scripts/fork-sync continue`.
- **A package lock is wrong.** Run `backend/scripts/update-python-lock.sh` in the
  worktree. It takes about 10 minutes.
- **An upstream test fails.** The tool runs only the fork tests. To find out
  whether an upstream test failure is new, run the same test file on pure
  upstream, in a second worktree (`git worktree add --detach <path> <upstream
  commit>`), with the same venv.
