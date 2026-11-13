---
name: bundlewright
description: Build a deployable, self-verifying release bundle (source + docker-compose + optional observability + embedded credentials + launchers + SHA256SUMS + .tar.gz) from any repository, and verify it. Use when asked to package, bundle, release, hand off a deployable artifact, or when a bundle shipped with a wrong model/location, a failing checksum, or a launcher that would not run.
argument-hint: --version <vX.Y.Z> --image-tag <repo:tag> [--location <region>] [--live-location <region>]
---

# bundlewright

One stdlib-only CLI. Zero install required: run it from the bundlewright repo.

```bash
python -m bundlewright --self-test                  # 21 checks
python -m bundlewright --version
bundlewright ...                                    # if installed (uv tool install .)
```

## Build a bundle

```bash
python -m bundlewright \
  --repo <path-to-project> \
  --version v1.0.9-alpha-024 \
  --image-tag <image>:candidate-v1.0.9-alpha-024 \
  --location asia-south1 \
  --live-location us-central1 \
  --model vertex_ai/gemini-3.5-flash \
  --live-model gemini-live-2.5-flash-native-audio \
  --cors-origin http://127.0.0.1:3020 \
  --embed-secrets <repo>/secrets/<sa>.json \
  [--no-observability] [--dry-run] [--json]
```

Prints `size_kb=` and `sha256=` of the archive, and writes `<out-dir>/<bundle>.tar.gz`.

## Five invariants (each exists because a release broke without it)

1. **Literal compose values.** Model names, locations, ports and live settings are
   literal strings, not `${VAR:-default}`. A stray host env var otherwise
   overrides the bundle `.env` → `503 PROVIDER_UNAVAILABLE`. Only secrets stay
   interpolated.
2. **Checksums last.** Files → `SHA256SUMS` → `tar.gz` → hash. Otherwise the
   archive fails its own integrity check.
3. **Executable bits explicit.** `0755` for `.sh`/`.cmd`, `0644` otherwise.
   Windows tars lose the bit; `./launcher.sh up` then dies with *Permission denied*.
4. **Single-command deploy.** Ship BOTH `launcher.sh` (Linux/macOS) and
   `launcher.cmd` (Windows); they run `docker compose up --build` and wait for `/ready`.
5. **Model reality enforced.** Known-invalid model/location pairs are rejected;
   `--allow-unverified-models` is the explicit escape hatch.

## Model/location facts

- `asia-south1` publishes `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-2.5-flash`.
  It does **not** publish `gemini-3.8-flash`, `gemini-2.5-flash-lite`, `gemini-2.5-pro` (404).
- Gemini Live (`gemini-live-2.5-flash-native-audio`) is **not** published in
  `asia-south1` (WebSocket 1008). It works in `us-central1`, `europe-west4`,
  `us-east4`; the `global` endpoint does not serve it.
- Text/vision location and `GEMINI_LIVE_LOCATION` are independent settings.

## Verify the artifact (not the staging dir)

```bash
tar -xzf <bundle>.tar.gz -C /tmp/verify
cd /tmp/verify/<bundle>
sha256sum -c SHA256SUMS            # expect 0 bad
./launcher.sh up                   # or launcher.cmd up on Windows
curl -s localhost:7500/ready
```

Then the API matrix (adapt per project): health, ready, the analyze/improve/
enhance/match/optimize endpoints, conversation scratch + ticket, WS
`/api/v1/agent/live` (query must carry `session_id` + `candidate_id` + `ticket`),
and `set_voice_mode → gemini_live`.

Prefer the project's Postman collection when one exists. Postman cloud cannot
reach `127.0.0.1`; export and run locally:

```bash
newman run <collection>.json -e <env>.json --reporters cli --timeout-request 300000
```

## Failure table

| Symptom | Cause | Fix |
|---|---|---|
| `503 PROVIDER_UNAVAILABLE` after 80s+ | model absent in that location, or host env overrode compose | published model/location; keep compose literal |
| `./launcher.sh: Permission denied` | `.sh` not `0755` in the archive | rebuild with bundlewright |
| archive fails `SHA256SUMS` | sums generated before final edits | rebuild (ordering enforced) |
| WS `403` in a browser | origin not allowlisted, or query missing the `session_id`+`candidate_id`+`ticket` tuple | set `CORS_ORIGINS`; send the tuple |
| WS URL contains a space | `set VAR=value &&` in cmd leaves a trailing space | set env in PowerShell; keep `.trim()` in the client |

## Rules for the agent

- Never edit files inside the produced bundle to "fix" a deploy; change the
  source/config and rebuild, so `SHA256SUMS` stays true.
- Never commit the produced `.tar.gz` or the embedded `.env`/secrets to a
  repository; hand them over privately.
- Always report `size_kb` and `sha256` from the tool's own output, never computed
  by hand.
- After building, verify from the extracted archive. A green staging directory
  proves nothing about the artifact.
