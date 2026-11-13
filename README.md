# bundlewright

Build a **deployable, self-verifying release bundle** from a repository: source
context, `docker-compose.yml`, optional observability stack, embedded
credentials, launchers, manifest, `SHA256SUMS`, and a `.tar.gz` whose size and
SHA-256 are printed for handoff.

A *wright* is a maker. `bundlewright` makes bundles — nothing else.

- stdlib only, no dependencies
- deterministic output (byte-reproducible archives)
- refuses known-broken configurations instead of shipping them
- ships the archive it could verify itself

## Install

Zero-install (works from the repo root):

```bash
python -m bundlewright --self-test
```

Install to get the `bundlewright` command:

```bash
uv tool install .        # or: pipx install .
bundlewright --version
```

## Quick start

```bash
bundlewright \
  --repo . \
  --version v1.0.9-alpha-024 \
  --image-tag myapp-intelligence:candidate-v1.0.9-alpha-024 \
  --location asia-south1 \
  --live-location us-central1 \
  --model vertex_ai/gemini-3.5-flash \
  --live-model gemini-live-2.5-flash-native-audio \
  --cors-origin http://127.0.0.1:3020 \
  --embed-secrets secrets/gemini-sa.json
```

Output: `<out-dir>/<bundle>/` and `<out-dir>/<bundle>.tar.gz`, printing
`size_kb=` and `sha256=` of the archive.

Common flags:

```
--name --version --image-tag --out-dir --repo
--python-source --env-file --compose-template --port
--project-name --container-name --no-observability
--location --live-location --model --live-model --cors-origin
--embed-secrets PATH
--dry-run --json --allow-unverified-models --extract-dir
--self-test
```

`--dry-run` prints the plan and writes nothing. `--json` prints a machine-readable
result.

## What the bundle contains

```
<bundle>/
  application/          # source context: src/, templates/, Dockerfile, pyproject.toml, uv.lock
  observability/        # loki/, alloy/, grafana/ + provisioning (unless --no-observability)
  secrets/              # embedded credentials, when --embed-secrets is used
  .env  .env.example
  docker-compose.yml    # engine + loki + alloy + grafana
  deploy.sh deploy.ps1 launcher.sh launcher.cmd verify.sh verify.ps1
  VERSION.txt RELEASE_MANIFEST.md SECURITY.md SHA256SUMS
```

## Invariants (each one exists because a release broke without it)

1. **Literal compose values.** Model names, locations, ports and live settings
   are written as literal strings, not `${VAR:-default}`. A stray host
   environment variable otherwise overrides the bundle `.env` and produces
   `503 PROVIDER_UNAVAILABLE` in production. Only secrets stay interpolated.
2. **Checksums last.** Write every file → `SHA256SUMS` → `tar.gz` → hash the
   archive. Generating sums before final edits makes the archive fail its own
   integrity check.
3. **Executable bits are explicit.** Windows has no exec bit to copy, so the
   packager sets `0755` for `.sh`/`.cmd` and `0644` otherwise. Without it,
   `./launcher.sh up` fails with *Permission denied* on Linux.
4. **Single-command deploy.** Both `launcher.sh up` (Linux/macOS) and
   `launcher.cmd up` (Windows) must exist; they run `docker compose up --build`
   and wait for `/ready`.
5. **Model reality is enforced.** Known-invalid model/location pairs are
   rejected; `--allow-unverified-models` is the explicit escape hatch.

## Model/location facts baked into the tool

| Location | Published |
|---|---|
| `asia-south1` | `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-2.5-flash` |
| `asia-south1` (absent) | `gemini-3.8-flash`, `gemini-2.5-flash-lite`, `gemini-2.5-pro` |
| Gemini Live | **not** in `asia-south1` (1008); works in `us-central1`, `europe-west4`, `us-east4` |

Text/vision location and `GEMINI_LIVE_LOCATION` are independent. A bundle can —
and usually should — use Mumbai for text and `us-central1` for voice.

## Verify a bundle

```bash
tar -xzf <bundle>.tar.gz -C /tmp/verify
cd /tmp/verify/<bundle>
sha256sum -c SHA256SUMS          # 0 bad
docker compose up -d --build
curl -s localhost:7500/ready
```

Then exercise the API surface. Prefer the project's Postman collection; Postman
cloud cannot reach `127.0.0.1`, so export it and run locally:

```bash
newman run <collection>.json -e <env>.json --reporters cli --timeout-request 300000
```

## How the bundle behaves when broken

| Symptom | Cause | Fix |
|---|---|---|
| `503 PROVIDER_UNAVAILABLE` after 80s+ | model absent in that location, or host env overrode compose | use a published model/location; keep compose literal |
| `./launcher.sh: Permission denied` | `.sh` not `0755` in the archive | rebuild with this tool |
| archive fails `SHA256SUMS` | sums generated before final edits | rebuild (ordering is enforced) |
| WS `403` from a browser | origin not allowlisted, or query missing `session_id`+`candidate_id`+`ticket` | set `CORS_ORIGINS`; send the full tuple |

## Development

```bash
python -m bundlewright --self-test     # 21 checks
pytest -q                              # wrapper test around the self-test
```

## License

MIT.
