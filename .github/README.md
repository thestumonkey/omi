<!-- [fork-only] GitHub shows this file on the fork's front page in place of
the root README.md, which stays byte-identical to upstream so syncs never
conflict on it. -->

<div align="center">

# Omi, self-hosted

### Omi on your own servers: your own sign-in, database, files and AI model

</div>

This is a fork of [BasedHardware/omi](https://github.com/BasedHardware/omi).
Omi records your conversations and your screen. It writes the words down, makes
summaries, tasks and memories, and gives you a chat that remembers them.

Upstream Omi uses Google services and Omi's cloud. This fork runs the same
upstream code on servers that you control. Your recordings, notes and memories
stay on your servers.

Upstream's own README is in [`README.md`](../README.md).

## What is different from upstream

| Part | Upstream | This fork |
|---|---|---|
| Sign-in | Firebase Auth | [Casdoor](https://casdoor.org) (OpenID Connect) |
| Database | Firestore | MongoDB |
| Files and audio | Google Cloud Storage | MinIO |
| AI model | Cloud models through Omi's gateway | Your own OpenAI-compatible server (llama.cpp, lemonade, Ollama) |
| Search vectors | Pinecone | Typesense, with local embeddings |
| Speaker detection | Hosted service | Local `diarizer` server |
| Plans and limits | Paid plans | None (`SELF_HOSTED=true`) |
| Speech to text | Deepgram | Deepgram (no change, needs a Deepgram key) |

These apps work with the fork:

- The backend, on Kubernetes.
- The macOS app.
- The iOS app (TestFlight or a direct install). There is no Android build.

## How the fork stays close to upstream

The fork does not rewrite upstream. It adds stand-ins:

- `backend/firebase_admin/` and `backend/database/mongo_firestore.py` answer the
  Firebase and Firestore calls from Casdoor and MongoDB.
- `backend/utils/other/minio_storage.py` answers the Google Cloud Storage calls
  from MinIO.
- Small blocks at the end of some upstream files switch in the fork code when
  `SELF_HOSTED` settings are set.

All other upstream files stay the same, byte for byte. Each fork-only file says
`[fork-only]` at the top. So most upstream changes merge with no conflict.

## Get started

All the steps are in [`SELFHOSTED.md`](../SELFHOSTED.md).

**You need:**

- A Kubernetes cluster (the fork uses microk8s) with an image registry.
- MongoDB, Redis and Casdoor.
- An OpenAI-compatible AI model server. An 8B model works. A GPU helps a lot.
- A Deepgram API key.
- A Mac with Xcode, for the Mac and iOS apps.

**Steps:**

1. Read [Keep secrets out of git](../SELFHOSTED.md#keep-secrets-out-of-git).
2. [Deploy the backend](../SELFHOSTED.md#deploy-the-backend): `make deploy`.
3. [Build the Mac app](../SELFHOSTED.md#build-the-mac-app):
   `make -C desktop run-selfhosted`.
4. [Ship the iOS app](../SELFHOSTED.md#ship-the-ios-app):
   `make mobile-ios-upload`.
5. Sign in with "Continue with Casdoor".
6. Make a short recording. Check that it gets a title, a summary and tasks.

Run `make` in the repo root to see all commands.

> **Note:** the deploy section is written for the maintainer's cluster. It has
> those host names and addresses in it. Change them to your own.

## Keep up to date

Upstream changes a lot each day. The tool `scripts/fork-sync` merges upstream,
fixes conflicts in fork code and runs the checks. It also lists new upstream
server settings that the fork does not have. Read
[Updates and upstream sync](../SELFHOSTED.md#updates-and-upstream-sync).

After each sync, set any new upstream setting on **all** backend servers:
`omi-backend`, `pusher` and `desktop-backend-py`.

## Known limits

- Tasks from desktop conversations go to "Suggested", and the local model
  usually rejects them. Tasks from phone conversations work.
- Desktop recordings with on-device speech to text do not close on silence.
  Stop the recording, or turn the audio off, to finish one.
- Place names (geolocation) do not work without a Google Maps key.

## License

MIT, the same as upstream. See [`LICENSE`](../LICENSE). Omi is made by
[Based Hardware](https://www.omi.me) and its contributors.
