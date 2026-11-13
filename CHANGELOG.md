# Changelog

## Unreleased

- CI: self-test on Python 3.9 / 3.12 / 3.13, pytest, sdist + wheel build.
- `SECURITY.md`, `CONTRIBUTING.md`, `.editorconfig`.
- Packaging metadata: project URLs, `dev` extra.

## 1.0.0 — 2026-11-14

First standalone release, extracted from the Decrypt Talent internal packager.

- Bundle layout: `application/`, `observability/`, `secrets/`, env, compose,
  launchers, manifest, `SHA256SUMS`, `.tar.gz`.
- Literal model/location values in compose; only secrets interpolated.
- Checksums generated last; archive hash and size printed in KB.
- Executable bits set explicitly for `.sh`/`.cmd`.
- `launcher.sh` and `launcher.cmd` with `up|start`, `down|stop`, `restart`.
- Known-invalid model/location pairs rejected; `--allow-unverified-models` escape hatch.
- `--self-test` (21 checks), `--dry-run`, `--json`.
