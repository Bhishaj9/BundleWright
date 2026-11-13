# Contributing

## Setup

```bash
git clone https://github.com/Bhishaj9/BundleWright.git
cd BundleWright
python -m bundlewright --self-test        # no install needed
python -m pip install pytest              # only for the test wrapper
```

## The gate

Every change must leave these green:

```bash
python -m bundlewright --self-test
python -m pytest -q
```

CI runs both on Python 3.9, 3.12 and 3.13, and builds an sdist + wheel.

## Invariants — do not weaken them

These are not style preferences; each exists because a release broke without it.

1. **Literal compose values.** Model, location, port and live settings are
   literal in the generated `docker-compose.yml`. Only secrets are interpolated.
   A `${VAR:-default}` here reintroduces silent runtime drift.
2. **Checksums last.** Files → `SHA256SUMS` → `tar.gz` → archive hash.
3. **Executable bits explicit.** `0755` for `.sh`/`.cmd`, `0644` otherwise.
4. **Single-command deploy.** Both `launcher.sh` and `launcher.cmd` must exist.
5. **Model reality.** Keep the known-invalid model/location table current; new
   knowledge goes in, escape hatches stay explicit (`--allow-unverified-models`).

If you add a behaviour, add a check to `run_self_test()` so the next person
cannot regress it silently.

## Style

- Python 3.9 compatible, stdlib only — no runtime dependencies.
- Deterministic output: sorted walks, fixed newlines (LF), UTF-8 without BOM,
  `mtime = 0` in archives.
- Keep the CLI surface small; prefer a flag over a config file, and a config
  file over an environment variable.

## Pull requests

- One concern per PR; do not mix a refactor with a behaviour change.
- Describe the failure the change prevents, not just the change.
- Update `CHANGELOG.md` under an `Unreleased` heading.
