#!/usr/bin/env python3
"""Project-agnostic deployment-bundle packager (standard library only).

Turns a FastAPI-style service repo into a self-contained, turnkey deployment
bundle:

    <bundle>/
      application/          source context (src/, templates/, Dockerfile,
                            pyproject.toml, uv.lock, .dockerignore)
      observability/         loki/, alloy/, grafana/, provisioning/
      secrets/               service credentials (optional)
      .env, .env.example
      docker-compose.yml
      deploy.sh, deploy.ps1, launcher.sh, verify.sh, verify.ps1
      VERSION.txt, RELEASE_MANIFEST.md, SECURITY.md, SHA256SUMS

...and emits a `.tar.gz` of that directory, reporting the archive size in KB
and its SHA-256.

Design rules encoded here (each one comes from a real production defect):

 1. LITERAL ENV — model and location values are written into
    docker-compose.yml as literal strings, never as `${VAR:-default}`. Compose
    interpolation resolves the operator's shell environment *before* the bundle
    `.env`, so an exported LITELLM_* / VERTEXAI_LOCATION silently overrode a
    bundle and produced 503 PROVIDER_UNAVAILABLE. Only secrets
    (INTELLIGENCE_API_KEY) stay interpolated.
 2. SUMS LAST — all files are written first, SHA256SUMS is generated after
    every other file exists, then the archive is created, then the archive is
    hashed. Generating sums before final edits made the archive fail its own
    integrity check.
 3. CONTAINER NAME — `container_name` is derived from the compose project name
    (or omitted entirely) so two bundles can run side by side.
 4. EXPLICIT EXTRACT DIR — a temp extraction directory outside the workspace is
    an explicit CLI argument (`--extract-dir`), never a hardcoded path.
 5. MODEL REALITY — a small KNOWN_INVALID table rejects model/location pairs
    that are known not to exist upstream, with `--allow-unverified-models` as
    the escape hatch.

Usage:
    python scripts/bundle_packager.py --repo . --version v1.0.0 --image-tag app:v1.0.0

Self-check:
    python scripts/bundle_packager.py --self-test
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from typing import Any, Iterable, NamedTuple

# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

#: Newline used for generated POSIX-ish text files.
LF = "\n"
#: Newline used for generated PowerShell scripts (matches the proven bundle).
CRLF = "\r\n"

#: Directory names never copied into the bundle.
SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".git",
        ".venv",
        "node_modules",
        ".nox",
        ".tox",
    }
)

#: File suffixes never copied into the bundle.
SKIP_FILE_SUFFIXES = (".pyc", ".pyo", ".pyd")

#: Files in the observability source tree that are not part of a bundle.
OBSERVABILITY_IGNORE = ("docker-compose.observability.yml", "verify_exposure.sh", "README.md")

#: Env keys allowed to be interpolated in docker-compose.yml. Everything else
#: bundle-critical is literal (see the module docstring, rule 1).
INTERPOLATED_ENV_KEYS = frozenset({"INTELLIGENCE_API_KEY"})

#: Env keys that are redacted in `.env.example`.
_SECRET_KEY_SUFFIXES = ("_API_KEY", "_PASSWORD", "_SECRET", "_TOKEN")

#: Default `.dockerignore` written into the build context.
DEFAULT_DOCKERIGNORE = """__pycache__/
*.py[cod]
.venv/
.env
.env.*
secrets/
storage/
*.log
.pytest_cache/
.ruff_cache/
.mypy_cache/
.git/
tests/
docs/
*.pdf
*.zip
*.tar
*.tar.gz
"""


# ---------------------------------------------------------------------------
# Defect 5 — model/location pairs known not to exist upstream
# ---------------------------------------------------------------------------


class KnownInvalid(NamedTuple):
    """A (model, location) pair that is known to fail against the real API."""

    models: tuple[str, ...]
    locations: tuple[str, ...]
    detail: str


#: Two facts proven against live Vertex AI. Unknown pairs are *not* rejected —
#: only these recorded failures are, so the table stays honest.
KNOWN_INVALID: tuple[KnownInvalid, ...] = (
    KnownInvalid(
        models=("gemini-3.8-flash",),
        locations=("asia-south1",),
        detail=(
            "gemini-3.8-flash does not exist in asia-south1 (Vertex returns 404). "
            "Models available in asia-south1: gemini-3.5-flash, "
            "gemini-3.5-flash-lite, gemini-2.5-flash."
        ),
    ),
    KnownInvalid(
        models=("gemini-live-2.5-flash-native-audio",),
        locations=("asia-south1",),
        detail=(
            "Gemini Live native-audio is not available in asia-south1 (WebSocket "
            "close 1008). It works in us-central1, europe-west4 and us-east4. "
            "Set locations.live independently of locations.text."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_ENDPOINTS: tuple[dict[str, str], ...] = (
    {"method": "GET", "path": "/health", "purpose": "process liveness"},
    {"method": "GET", "path": "/ready", "purpose": "readiness + credential/provider checks"},
    {"method": "POST", "path": "/api/v1/resume/analyze", "purpose": "resume analysis"},
    {"method": "POST", "path": "/api/v1/resume/analyze/manual", "purpose": "analysis without upload extraction"},
    {"method": "POST", "path": "/api/v1/resume/review", "purpose": "section review"},
    {"method": "POST", "path": "/api/v1/resume/improve", "purpose": "grounded improvement pass"},
    {"method": "POST", "path": "/api/v1/resume/enhance", "purpose": "two source-grounded section suggestions"},
    {"method": "POST", "path": "/api/v1/job/analyze", "purpose": "job description analysis"},
    {"method": "POST", "path": "/api/v1/job/match", "purpose": "resume/job matching"},
    {"method": "POST", "path": "/api/v1/job/optimize", "purpose": "grounded JD optimization"},
    {"method": "POST", "path": "/api/v1/conversation/session", "purpose": "conversation session create"},
    {"method": "POST", "path": "/api/v1/conversation/session/scratch", "purpose": "conversation scratch session"},
    {"method": "POST", "path": "/api/v1/conversation/ticket", "purpose": "session ticket issue"},
    {"method": "POST", "path": "/api/v1/conversation/message", "purpose": "conversation turn"},
    {"method": "WS", "path": "/api/v1/conversation/ws/{session_id}", "purpose": "Gemini Live relay"},
    {"method": "WS", "path": "/api/v1/conversation/agent/live", "purpose": "agent live socket"},
)

DEFAULT_CONFIG: dict[str, Any] = {
    "name": "",
    "version": "",
    "app_version": "",
    "image_tag": "",
    "extract_dir": "",
    "port": 7500,
    "project_name": "",
    # "{project}" is substituted; set to "" to omit container_name entirely so
    # Compose auto-names the container (best for side-by-side runs).
    "container_name": "{project}-intelligence",
    "python_source": "src",
    "include_observability": True,
    "observability_source": "deployment/observability",
    "secret_path": None,
    "secret_name": "",
    "env_file": ".env",
    "compose_template": "builtin",
    "out_dir": "dist/bundles",
    "log_level": "INFO",
    "cors_origins": [],
    # Env keys copied literally from the resolved .env into compose. The
    # container never sees the bundle-root .env (the image's WORKDIR has no
    # .env), so anything the service needs at runtime must be listed here.
    "compose_passthrough": ["VERTEX_PROJECT"],
    "exclusions": ["tests/", "postman/"],
    "purpose": "",
    "credential_model": "",
    "dockerignore": DEFAULT_DOCKERIGNORE,
    "endpoints": [dict(item) for item in DEFAULT_ENDPOINTS],
    "models": {
        "provider": "vertex_ai",
        "primary": "gemini-3.5-flash",
        "reasoning": "",
        "fallbacks": ["gemini-2.5-flash"],
        "vision": "",
        "vision_fallbacks": [],
        "live": "gemini-live-2.5-flash-native-audio",
        "fallback_thinking_level": "LOW",
    },
    "locations": {
        "text": "asia-south1",
        "live": "us-central1",
        # Optional: emitted only when set, so we never invent a conversation
        # region the operator did not ask for.
        "conversation": "",
    },
    "features": {
        "improve_second_pass": False,
        "consolidated_analyze": True,
    },
}


class PackagingError(Exception):
    """A validation or packaging failure that must stop the run."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sha256_file(path: str) -> str:
    """Return the lowercase hex SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 256)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _as_lf(text: str) -> str:
    """Normalise any newline convention to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_text(path: str, text: str, newline: str = LF) -> None:
    """Write UTF-8 without BOM, with an explicit deterministic newline."""
    normalised = _as_lf(text)
    if newline != LF:
        normalised = normalised.replace(LF, newline)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(normalised.encode("utf-8"))


def read_env_file(path: str) -> list[tuple[str | None, str]]:
    """Parse a dotenv file into (key, raw_line) pairs.

    `key` is None for comments and blank lines. Values are not evaluated; the
    raw line is preserved so untouched settings survive byte-for-byte.
    """
    pairs: list[tuple[str | None, str]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                pairs.append((None, line))
                continue
            pairs.append((stripped.split("=", 1)[0].strip(), line))
    return pairs


def env_lookup(pairs: Iterable[tuple[str | None, str]]) -> dict[str, str]:
    """Return a plain dict of key -> value from parsed env pairs."""
    values: dict[str, str] = {}
    for key, line in pairs:
        if key is None:
            continue
        values[key] = line.split("=", 1)[1] if "=" in line else ""
    return values


def is_secret_key(key: str) -> bool:
    """True when an env key names a credential."""
    upper = key.upper()
    return any(upper.endswith(suffix) for suffix in _SECRET_KEY_SUFFIXES)


def qualify_model(model: str, provider: str) -> str:
    """Prefix a bare model name with the provider; leave qualified names alone."""
    model = (model or "").strip()
    if not model:
        return ""
    return model if "/" in model else f"{provider}/{model}"


def _slugify(value: str) -> str:
    """Lowercase, dash-separated identifier suitable for compose names."""
    out = []
    for char in value.strip().lower():
        if char.isalnum():
            out.append(char)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "bundle"


def iter_tree(root: str) -> list[tuple[str, bool]]:
    """Return (relative posix path, is_dir) for `root`, deterministically sorted."""
    entries: list[tuple[str, bool]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        for name in dirnames:
            entries.append((os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/"), True))
        for name in filenames:
            entries.append((os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/"), False))
    return sorted(entries, key=lambda entry: entry[0])


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    """shutil.copytree ignore hook: drop caches and build artefacts."""
    ignored = set()
    for name in names:
        if name in SKIP_DIR_NAMES or name.endswith(SKIP_FILE_SUFFIXES):
            ignored.add(name)
    return ignored


def _copy_ignore_observability(directory: str, names: list[str]) -> set[str]:
    ignored = _copy_ignore(directory, names)
    ignored.update(name for name in names if name in OBSERVABILITY_IGNORE)
    return ignored


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_model_matrix(models: dict[str, Any], locations: dict[str, Any]) -> list[str]:
    """Return a list of KNOWN_INVALID violations for the configured matrix."""
    problems: list[str] = []
    provider = models.get("provider", "")

    text_pairs: list[tuple[str, str]] = []
    for key in ("primary", "reasoning", "vision"):
        value = (models.get(key) or "").strip()
        if value:
            text_pairs.append((f"models.{key}", qualify_model(value, provider)))
    for key in ("fallbacks", "vision_fallbacks"):
        for index, value in enumerate(models.get(key) or []):
            if (value or "").strip():
                text_pairs.append((f"models.{key}[{index}]", qualify_model(value, provider)))

    text_location = (locations.get("text") or "").strip()
    live_location = (locations.get("live") or "").strip()
    live_model = qualify_model(models.get("live", ""), provider)

    for label, model in text_pairs:
        problems.extend(_known_invalid(label, model, text_location))
    if live_model:
        problems.extend(_known_invalid("models.live", live_model, live_location))
    return problems


def _known_invalid(label: str, model: str, location: str) -> list[str]:
    bare = model.split("/")[-1]
    for rule in KNOWN_INVALID:
        if bare in rule.models and location in rule.locations:
            return [f"{label}: {model} @ {location} -> {rule.detail}"]
    return []


def validate_compose_template(template_path: str, image_tag: str) -> None:
    """Fail when a custom compose template does not carry the configured image tag."""
    with open(template_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    if "{{IMAGE_TAG}}" in text or image_tag in text:
        return
    raise PackagingError(
        f"compose template '{template_path}' does not reference the configured image tag "
        f"'{image_tag}'. Add the {{{{IMAGE_TAG}}}} placeholder or the literal tag."
    )


def validate_config(config: dict[str, Any], repo: str) -> None:
    """Fail fast on missing required inputs and missing source paths."""
    missing: list[str] = []
    if not config.get("version"):
        missing.append("version (--version or config 'version')")
    if not config.get("image_tag"):
        missing.append("image_tag (--image-tag or config 'image_tag')")
    if missing:
        raise PackagingError("missing required input(s): " + "; ".join(missing))

    if not os.path.isdir(repo):
        raise PackagingError(f"repo does not exist or is not a directory: {repo}")

    python_source = os.path.join(repo, str(config["python_source"]))
    if not os.path.isdir(python_source):
        raise PackagingError(f"python source directory not found: {python_source}")

    for required in ("Dockerfile", "pyproject.toml", "uv.lock"):
        path = os.path.join(repo, required)
        if not os.path.isfile(path):
            raise PackagingError(f"required source file not found: {path}")

    env_file = os.path.join(repo, str(config["env_file"]))
    if not os.path.isfile(env_file):
        raise PackagingError(f"env file not found: {env_file}")

    if config.get("include_observability"):
        obs = os.path.join(repo, str(config["observability_source"]))
        if not os.path.isdir(obs):
            raise PackagingError(
                f"observability source not found: {obs} "
                "(pass --no-observability to build without it)"
            )

    template = config.get("compose_template") or "builtin"
    if template != "builtin":
        if not os.path.isfile(template):
            raise PackagingError(f"compose template not found: {template}")
        validate_compose_template(template, str(config["image_tag"]))

    secret = (config.get("secret_path") or "").strip()
    if secret and not os.path.isfile(secret):
        raise PackagingError(f"embedded secret not found: {secret}")


# ---------------------------------------------------------------------------
# Compose rendering
# ---------------------------------------------------------------------------


def render_compose(config: dict[str, Any], env_values: dict[str, str]) -> str:
    """Render docker-compose.yml with literal model/location values."""
    models = config["models"]
    locations = config["locations"]
    provider = models.get("provider", "vertex_ai")
    project = config["_project"]
    slug = _slugify(project)
    network = f"{slug}-net"
    port = int(config["port"])
    secret_name = config["_secret_name"]

    primary = qualify_model(models.get("primary", ""), provider)
    reasoning = qualify_model(models.get("reasoning", ""), provider) or primary
    fallbacks = ",".join(qualify_model(m, provider) for m in (models.get("fallbacks") or []) if m)
    vision = qualify_model(models.get("vision", ""), provider) or primary
    vision_fallbacks = ",".join(
        qualify_model(m, provider) for m in (models.get("vision_fallbacks") or []) if m
    )
    live = qualify_model(models.get("live", ""), provider)

    # Ordered literal environment. Model and location values are plain strings:
    # a `${...}` here would let the host shell win (defect 1).
    env_lines: list[tuple[str, str]] = [
        ("ENVIRONMENT", "production"),
        ("PYTHONUNBUFFERED", "1"),
        ("LOG_LEVEL", str(config["log_level"])),
        ("SERVICE_NAME", "intelligence-engine"),
        ("HOST_PORT", str(port)),
        ("STORAGE_DIR", "/app/storage"),
        ("VERTEX_CREDENTIALS_PATH", f"/run/secrets/{secret_name}"),
        ("GOOGLE_APPLICATION_CREDENTIALS", f"/run/secrets/{secret_name}"),
        ("LITELLM_PROVIDER", provider),
        ("LITELLM_DEFAULT_MODEL", primary),
        ("LITELLM_REASONING_MODEL", reasoning),
        ("LITELLM_FALLBACK_MODEL", fallbacks),
        ("LITELLM_VISION_MODEL", vision),
        ("LITELLM_VISION_FALLBACK_MODEL", vision_fallbacks),
        ("DEFAULT_INFERENCE_REGION", str(locations.get("text", ""))),
        ("VERTEXAI_LOCATION", str(locations.get("text", ""))),
        ("GEMINI_LIVE_LOCATION", str(locations.get("live", ""))),
        ("GEMINI_FALLBACK_THINKING_LEVEL", str(models.get("fallback_thinking_level", "LOW"))),
        (
            "ENABLE_IMPROVE_SECOND_PASS",
            "true" if config["features"].get("improve_second_pass") else "false",
        ),
        (
            "ENABLE_CONSOLIDATED_ANALYZE",
            "true" if config["features"].get("consolidated_analyze") else "false",
        ),
        ("CORS_ORIGINS", ",".join(config.get("cors_origins") or [])),
    ]
    if live:
        env_lines.append(("GEMINI_LIVE_MODEL", live))
    conversation_location = (locations.get("conversation") or "").strip()
    if conversation_location:
        env_lines.append(("CONVERSATION_VERTEXAI_LOCATION", conversation_location))

    emitted = {key for key, _ in env_lines}
    for key in config.get("compose_passthrough") or []:
        if key in emitted or key in INTERPOLATED_ENV_KEYS:
            continue
        value = env_values.get(key, "").strip()
        if value:
            env_lines.append((key, value))
            emitted.add(key)

    literal_block = "".join(f"      - {key}={value}\n" for key, value in env_lines)
    secret_block = "".join(
        f"      - {key}=${{{key}}}\n" for key in sorted(INTERPOLATED_ENV_KEYS)
    )

    container_name = (config.get("container_name") or "").replace("{project}", project).strip()
    container_line = f"    container_name: {container_name}\n" if container_name else ""

    parts: list[str] = []
    parts.append(
        "# " + "=" * 76 + "\n"
        f"# {config['_bundle_name']} ({config['version']})\n"
        "#\n"
        "# Generated by scripts/bundle_packager.py. Edit the packager config and\n"
        "# repackage instead of editing this file by hand.\n"
        "#\n"
        "# INVARIANT (defect 1): every model and location value below is a LITERAL\n"
        "# string. Nothing model- or location-related is interpolated from the host\n"
        "# shell, so an exported LITELLM_* / VERTEXAI_LOCATION cannot override this\n"
        "# bundle. Only INTELLIGENCE_API_KEY is interpolated.\n"
        "#\n"
        "# INVARIANT (defect 2): SHA256SUMS is written after every other file and\n"
        "# covers the files listed in it, except itself.\n"
        "# " + "=" * 76 + "\n"
    )
    parts.append(f"name: ${{COMPOSE_PROJECT_NAME:-{project}}}\n\n")
    parts.append(f"secrets:\n  service_creds:\n    file: ./secrets/{secret_name}\n\n")
    parts.append("services:\n  intelligence-service:\n")
    parts.append(f"    image: {config['image_tag']}\n")
    parts.append("    build:\n      context: ./application\n      dockerfile: Dockerfile\n")
    parts.append(container_line)
    parts.append("    restart: unless-stopped\n")
    parts.append(f'    ports:\n      - "${{HOST_PORT:-{port}}}:{port}"\n')
    parts.append("    environment:\n")
    parts.append(literal_block)
    parts.append(secret_block)
    parts.append("    secrets:\n      - source: service_creds\n")
    parts.append(f"        target: {secret_name}\n")
    parts.append("    volumes:\n      - intelligence-storage:/app/storage\n")
    parts.append(f"      - ./secrets/{secret_name}:/run/secrets/{secret_name}:ro\n")
    parts.append(
        "    healthcheck:\n"
        '      test: ["CMD", "python", "-c", '
        f'"import urllib.request; urllib.request.urlopen(\'http://127.0.0.1:{port}/ready\')"]\n'
        "      interval: 10s\n"
        "      timeout: 5s\n"
        "      retries: 6\n"
        "      start_period: 20s\n"
    )
    parts.append("    labels:\n")
    parts.append(f'      logging.jobname: "{slug}-intelligence"\n')
    parts.append(f"    networks:\n      - {network}\n")

    if config.get("include_observability"):
        parts.append(
            "\n  loki:\n"
            "    image: grafana/loki:3.5.0\n"
            f"    container_name: {slug}-loki\n"
            "    restart: unless-stopped\n"
            "    command: -config.file=/etc/loki/local-config.yaml\n"
            "    volumes:\n"
            "      - ./observability/loki/loki-config.yaml:/etc/loki/local-config.yaml:ro\n"
            "      - loki-data:/loki\n"
            '    ports:\n      - "127.0.0.1:3100:3100"\n'
            "    healthcheck:\n"
            '      test: ["CMD-SHELL", "wget -q --spider http://127.0.0.1:3100/ready || exit 1"]\n'
            "      interval: 10s\n"
            "      timeout: 5s\n"
            "      retries: 5\n"
            "      start_period: 10s\n"
            f"    networks:\n      - {network}\n"
        )
        parts.append(
            "\n  alloy:\n"
            "    image: grafana/alloy:v1.9.2\n"
            f"    container_name: {slug}-alloy\n"
            "    restart: unless-stopped\n"
            "    command: run /etc/alloy/config.alloy --storage.path=/var/lib/alloy/data\n"
            "    volumes:\n"
            "      - ./observability/alloy/config.alloy:/etc/alloy/config.alloy:ro\n"
            "      - /var/run/docker.sock:/var/run/docker.sock:ro\n"
            "      - alloy-data:/var/lib/alloy/data\n"
            '    ports:\n      - "127.0.0.1:12345:12345"\n'
            "    depends_on:\n      - loki\n"
            f"    networks:\n      - {network}\n"
        )
        parts.append(
            "\n  grafana:\n"
            "    image: grafana/grafana:11.6.0\n"
            f"    container_name: {slug}-grafana\n"
            "    restart: unless-stopped\n"
            "    environment:\n"
            "      - GF_SECURITY_ADMIN_USER=admin\n"
            "      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD:-admin}\n"
            "      - GF_USERS_ALLOW_SIGN_UP=false\n"
            "    volumes:\n"
            "      - ./observability/provisioning:/etc/grafana/provisioning:ro\n"
            "      - ./observability/grafana/dashboards:/etc/grafana/dashboards:ro\n"
            "      - grafana-data:/var/lib/grafana\n"
            '    ports:\n      - "127.0.0.1:3000:3000"\n'
            "    depends_on:\n      - loki\n"
            "    healthcheck:\n"
            '      test: ["CMD-SHELL", "wget -q --spider http://127.0.0.1:3000/api/health || exit 1"]\n'
            "      interval: 10s\n"
            "      timeout: 5s\n"
            "      retries: 5\n"
            "      start_period: 15s\n"
            f"    networks:\n      - {network}\n"
        )

    parts.append(f"\nnetworks:\n  {network}:\n    driver: bridge\n")
    volumes = ["intelligence-storage"]
    if config.get("include_observability"):
        volumes.extend(["loki-data", "alloy-data", "grafana-data"])
    parts.append("\nvolumes:\n")
    for volume in volumes:
        parts.append(f"  {volume}:\n")

    return "".join(parts)


def apply_compose_placeholders(text: str, config: dict[str, Any]) -> str:
    """Substitute the documented placeholders in a custom compose template."""
    replacements = {
        "{{IMAGE_TAG}}": str(config["image_tag"]),
        "{{PORT}}": str(config["port"]),
        "{{PROJECT}}": config["_project"],
        "{{BUNDLE_NAME}}": config["_bundle_name"],
        "{{VERSION}}": str(config["version"]),
        "{{SECRET_NAME}}": config["_secret_name"],
    }
    for token, value in replacements.items():
        text = text.replace(token, value)
    return text


# ---------------------------------------------------------------------------
# Script and document rendering
# ---------------------------------------------------------------------------


def render_scripts(config: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Return {filename: (content, newline)} for the turnkey launchers."""
    bundle = config["_bundle_name"]
    port = int(config["port"])

    deploy_sh = f"""#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

CMD="${{1:-up}}"

case "$CMD" in
  up|start)
    echo "==> Deploying {bundle}..."
    docker compose up -d --build
    echo "==> Running readiness probe..."
    ./verify.sh
    ;;
  down|stop)
    echo "==> Stopping {bundle}..."
    docker compose down
    ;;
  restart)
    echo "==> Restarting {bundle}..."
    docker compose down
    docker compose up -d --build
    ./verify.sh
    ;;
  *)
    docker compose "$@"
    ;;
esac
"""

    launcher_sh = f"""#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

CMD="${{1:-up}}"

case "$CMD" in
  up|start)
    echo "==> Deploying {bundle}..."
    docker compose up -d --build
    echo "==> Waiting for intelligence-service to report healthy..."
    for _ in $(seq 1 30); do
      if docker compose ps intelligence-service | grep -q "healthy"; then
        echo "==> intelligence-service is healthy."
        break
      fi
      sleep 2
    done
    ;;
  down|stop)
    echo "==> Stopping {bundle}..."
    docker compose down
    ;;
  restart)
    echo "==> Restarting {bundle}..."
    docker compose down
    docker compose up -d --build
    ;;
  *)
    docker compose "$@"
    ;;
esac
"""

    verify_sh = f"""#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

port="${{HOST_PORT:-{port}}}"
url="http://127.0.0.1:${{port}}/ready"
echo "==> Probing readiness on $url ..."

for _ in $(seq 1 30); do
  if curl -s -f "$url" > /dev/null; then
    echo "[OK] Service is ready on http://127.0.0.1:${{port}}"
    exit 0
  fi
  echo "    Waiting for /ready ..."
  sleep 2
done

echo "[FAIL] /ready did not pass within 60s" >&2
exit 1
"""

    deploy_ps1 = f"""# {bundle} - PowerShell deployment launcher
param(
    [string]$Action = "up"
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

switch ($Action.ToLower()) {{
    "up" {{
        Write-Host "==> Deploying {bundle}..."
        docker compose up -d --build
        & .\\verify.ps1
    }}
    "down" {{
        Write-Host "==> Stopping {bundle}..."
        docker compose down
    }}
    "restart" {{
        Write-Host "==> Restarting {bundle}..."
        docker compose down
        docker compose up -d --build
        & .\\verify.ps1
    }}
    default {{
        docker compose $Action
    }}
}}
"""

    verify_ps1 = f"""# {bundle} - PowerShell readiness probe
$port = $env:HOST_PORT
if (-not $port) {{ $port = "{port}" }}

$url = "http://127.0.0.1:$port/ready"
Write-Host "==> Probing readiness on $url ..."

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {{
    try {{
        $resp = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 3 -ErrorAction Stop
        if ($resp.status -eq "ready") {{
            $ready = $true
            break
        }}
    }} catch {{
        # keep waiting
    }}
    Start-Sleep -Seconds 2
}}

if (-not $ready) {{
    Write-Error "Service on port $port failed to become ready within 60s."
    exit 1
}}
Write-Host "[OK] Service on port $port is healthy and ready."
exit 0
"""

    # Windows single-command launcher. Built by substitution (not f-string) so the
    # batch/PowerShell braces stay literal.
    launcher_cmd = """@echo off
setlocal EnableExtensions
pushd "%~dp0"
set "CMD=%~1"
if /I "%CMD%"=="" set "CMD=up"
if /I "%CMD%"=="up"      goto up
if /I "%CMD%"=="start"   goto up
if /I "%CMD%"=="down"    goto down
if /I "%CMD%"=="stop"    goto down
if /I "%CMD%"=="restart" goto restart
docker compose %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:up
echo ==^> Deploying __BUNDLE__...
docker compose up -d --build
if errorlevel 1 ( echo [FAIL] docker compose up failed & popd & exit /b 1 )
call :wait_ready
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:down
echo ==^> Stopping __BUNDLE__...
docker compose down
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:restart
echo ==^> Restarting __BUNDLE__...
docker compose down
docker compose up -d --build
if errorlevel 1 ( echo [FAIL] docker compose up failed & popd & exit /b 1 )
call :wait_ready
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:wait_ready
set "PORT=__PORT__"
for /f "usebackq tokens=1,2 delims==" %%A in (`findstr /b /c:"HOST_PORT=" ".env" 2^>nul`) do set "PORT=%%B"
echo ==^> Waiting for /ready on port %PORT%...
powershell -NoProfile -ExecutionPolicy Bypass -Command "for($i=0;$i -lt 60;$i++){try{Invoke-RestMethod -Uri ('http://127.0.0.1:' + '%PORT%' + '/ready') -TimeoutSec 3 | Out-Null; Write-Host '[OK] Service is ready'; exit 0}catch{Start-Sleep -Seconds 2}}; Write-Host '[FAIL] timed out waiting for /ready'; exit 1"
exit /b %ERRORLEVEL%
""".replace("__BUNDLE__", bundle).replace("__PORT__", str(port))

    return {
        "deploy.sh": (deploy_sh, LF),
        "launcher.sh": (launcher_sh, LF),
        "launcher.cmd": (launcher_cmd, CRLF),
        "verify.sh": (verify_sh, LF),
        "deploy.ps1": (deploy_ps1, CRLF),
        "verify.ps1": (verify_ps1, CRLF),
    }


def render_version_txt(config: dict[str, Any]) -> str:
    """Render VERSION.txt: version, port, model matrix, endpoints, exclusions."""
    models = config["models"]
    locations = config["locations"]
    provider = models.get("provider", "vertex_ai")

    def qual(value: str) -> str:
        return qualify_model(value, provider) or "(inherit primary)"

    lines: list[str] = []
    lines.append(f"Bundle: {config['_bundle_name']}")
    lines.append(f"Bundle Version: {config['version']}")
    lines.append(f"Application Version: {config.get('app_version') or config['version']}")
    lines.append(f"Target Port: {config['port']}")
    lines.append(f"Image: {config['image_tag']}")
    lines.append(f"Compose Project: {config['_project']}")
    lines.append(f"Container Name: {config['_container_name'] or '(compose-managed, no fixed name)'}")
    if config.get("purpose"):
        lines.append(f"Purpose: {config['purpose']}")
    lines.append("")
    lines.append("Model Matrix:")
    lines.append(f"  Provider: {provider}")
    lines.append(f"  Primary: {qual(models.get('primary', ''))}")
    lines.append(f"  Reasoning: {qual(models.get('reasoning', ''))}")
    fallbacks = ", ".join(qual(m) for m in (models.get("fallbacks") or [])) or "(none)"
    lines.append(f"  Fallbacks: {fallbacks}")
    lines.append(f"  Vision: {qual(models.get('vision', ''))}")
    vision_fallbacks = ", ".join(qual(m) for m in (models.get("vision_fallbacks") or [])) or "(none)"
    lines.append(f"  Vision Fallbacks: {vision_fallbacks}")
    lines.append(f"  Live: {qual(models.get('live', ''))}")
    lines.append(f"  Fallback Thinking Level: {models.get('fallback_thinking_level', 'LOW')}")
    lines.append("")
    lines.append("Locations:")
    lines.append(f"  Text/Vision (VERTEXAI_LOCATION): {locations.get('text', '')}")
    lines.append(f"  Live (GEMINI_LIVE_LOCATION): {locations.get('live', '')}")
    if locations.get("conversation"):
        lines.append(f"  Conversation: {locations['conversation']}")
    lines.append("  Note: text and live locations are independent; Gemini Live is not")
    lines.append("        served from every text region.")
    lines.append("")
    origins = config.get("cors_origins") or []
    lines.append(f"CORS Origins: {', '.join(origins) if origins else '(none configured)'}")
    lines.append("")
    lines.append("Endpoints:")
    for endpoint in config.get("endpoints") or []:
        lines.append(
            f"  {endpoint.get('method', ''):<5} {endpoint.get('path', ''):<45} "
            f"{endpoint.get('purpose', '')}"
        )
    lines.append("")
    lines.append(f"Observability Stack: {'loki + alloy + grafana' if config.get('include_observability') else 'disabled'}")
    exclusions = config.get("exclusions") or []
    lines.append(f"Exclusions: {', '.join(exclusions) if exclusions else '(none)'}")
    credential_model = config.get("credential_model") or (
        "Internal authorized test credentials embedded" if config["_secret_embedded"]
        else "External - operator supplies the credential file before deploy"
    )
    lines.append(f"Credential Model: {credential_model}")
    lines.append("")
    lines.append("Packaging Invariants:")
    lines.append("  - Model and location values in docker-compose.yml are LITERAL strings;")
    lines.append("    only INTELLIGENCE_API_KEY is interpolated. An exported LITELLM_* or")
    lines.append("    VERTEXAI_LOCATION in the operator shell cannot override this bundle.")
    lines.append("  - SHA256SUMS is generated after every other file and covers every file")
    lines.append("    in the bundle except SHA256SUMS itself.")
    return "\n".join(lines) + "\n"


def render_release_manifest(config: dict[str, Any], repo: str, file_count: int) -> str:
    models = config["models"]
    locations = config["locations"]
    provider = models.get("provider", "vertex_ai")

    def qual(value: str) -> str:
        return qualify_model(value, provider) or "(inherit primary)"

    lines = [
        f"# Release Manifest - {config['_bundle_name']}",
        "",
        f"- **Bundle Version:** {config['version']}",
        f"- **Application Version:** {config.get('app_version') or config['version']}",
        f"- **Image Tag:** `{config['image_tag']}`",
        f"- **Compose Project:** `{config['_project']}`",
        f"- **Container Name:** {config['_container_name'] or '(compose-managed)'}",
        f"- **Source Repository:** `{repo}`",
        f"- **Target Port:** {config['port']}",
        f"- **Primary Model:** `{qual(models.get('primary', ''))}`",
        f"- **Fallbacks:** `{', '.join(qual(m) for m in (models.get('fallbacks') or [])) or '(none)'}`",
        f"- **Vision Model:** `{qual(models.get('vision', ''))}`",
        f"- **Live Model:** `{qual(models.get('live', ''))}`",
        f"- **Text Location:** `{locations.get('text', '')}`",
        f"- **Live Location:** `{locations.get('live', '')}`",
        f"- **Observability:** {'loki 3.5 + alloy 1.9 + grafana 11.6' if config.get('include_observability') else 'disabled'}",
        f"- **Files (excluding SHA256SUMS):** {file_count}",
        "- **Packaging Engine:** Python standard library only (UTF-8 without BOM)",
        "",
        "## Packaging invariants",
        "",
        "1. Model and location values are literal in `docker-compose.yml`; only",
        "   `INTELLIGENCE_API_KEY` is interpolated.",
        "2. `SHA256SUMS` is written last, before archive creation; the archive is",
        "   hashed after creation.",
        "3. `container_name` derives from the compose project name, or is omitted,",
        "   so bundles can run side by side.",
        "",
        "## Verification",
        "",
        "```",
        "sha256sum -c SHA256SUMS      # on macOS: shasum -a 256 -c SHA256SUMS",
        "./verify.sh                  # or: pwsh ./verify.ps1",
        "```",
    ]
    return "\n".join(lines) + "\n"


def render_security_md(config: dict[str, Any]) -> str:
    lines = [
        "# Security & Confidentiality Notice",
        "",
        "## SENSITIVE ARTIFACT",
        "",
    ]
    if config["_secret_embedded"]:
        lines.extend(
            [
                "This archive embeds pre-configured credentials:",
                "",
                f"- `secrets/{config['_secret_name']}`",
                "- `.env` (provider API keys, service API key)",
                "",
                "Do not distribute outside authorized channels. Do not commit this",
                "archive or its extracted contents to public version control.",
            ]
        )
    else:
        lines.extend(
            [
                "This archive contains configuration but no credential material.",
                "",
                f"Before deploying, place the service credential file at",
                f"`secrets/{config['_secret_name']}`. The stack will not start without it.",
            ]
        )
    lines.extend(
        [
            "",
            "## Runtime exposure",
            "",
            "- Observability ports (3100, 12345, 3000) bind to 127.0.0.1 only.",
            f"- The engine port ({config['port']}) is published on "
            f"`${{HOST_PORT:-{config['port']}}}`;",
            "  restrict it at the network layer to trusted callers.",
        ]
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Packager
# ---------------------------------------------------------------------------


class BundlePackager:
    """Builds one bundle directory plus its .tar.gz artifact."""

    def __init__(self, config: dict[str, Any], repo: str, dry_run: bool = False) -> None:
        self.repo = os.path.abspath(repo)
        self.config = config
        self.dry_run = dry_run
        # Derived names, resolved once so renderers stay pure.
        bundle_name = config.get("name") or os.path.basename(self.repo.rstrip(os.sep))
        self.bundle_name = str(bundle_name)
        project = config.get("project_name") or self.bundle_name
        self.project = _slugify(str(project))
        secret_name = config.get("secret_name") or (
            os.path.basename(str(config["secret_path"])) if config.get("secret_path") else "service-creds.json"
        )
        self.secret_name = str(secret_name)
        self.secret_embedded = bool(str(config.get("secret_path") or "").strip())
        container = (config.get("container_name") or "").replace("{project}", self.project).strip()
        self.out_dir = os.path.abspath(str(config["out_dir"]))

        self.config = dict(config)
        self.config.update(
            {
                "_bundle_name": self.bundle_name,
                "_project": self.project,
                "_secret_name": self.secret_name,
                "_secret_embedded": self.secret_embedded,
                "_container_name": container,
            }
        )

        self.staging_dir = os.path.join(self.out_dir, self.bundle_name)
        self.archive_path = os.path.join(self.out_dir, f"{self.bundle_name}.tar.gz")

    # -- planning ----------------------------------------------------------

    def plan(self) -> list[str]:
        """Human-readable list of what a run would write."""
        steps = [
            f"clean staging dir        {self.staging_dir}",
            f"copy application context  <repo>/{self.config['python_source']} -> application/",
            "copy build files          Dockerfile, pyproject.toml, uv.lock, .dockerignore",
        ]
        if self.config.get("include_observability"):
            steps.append(
                f"copy observability        <repo>/{self.config['observability_source']} -> observability/"
            )
        else:
            steps.append("skip observability        (--no-observability)")
        if self.secret_embedded:
            steps.append(f"embed secret              {self.config['secret_path']} -> secrets/{self.secret_name}")
        steps.extend(
            [
                f"write .env/.env.example   HOST_PORT={self.config['port']} (literal overrides)",
                "write docker-compose.yml  literal model/location values",
                "write launchers           deploy/launcher/verify .sh + .ps1",
                "write VERSION.txt         version, port, model matrix, endpoints, exclusions",
                "write RELEASE_MANIFEST.md + SECURITY.md",
                f"write SHA256SUMS          LAST, over every file except itself",
                f"create archive            {self.archive_path}",
                "print size_kb + sha256    of the archive",
            ]
        )
        return steps

    # -- build steps -------------------------------------------------------

    def _build_application(self) -> None:
        app_dir = os.path.join(self.staging_dir, "application")
        source_name = os.path.basename(str(self.config["python_source"]).rstrip("/\\"))
        shutil.copytree(
            os.path.join(self.repo, str(self.config["python_source"])),
            os.path.join(app_dir, source_name),
            ignore=_copy_ignore,
        )

        templates_src = os.path.join(self.repo, "templates")
        if os.path.isdir(templates_src):
            shutil.copytree(templates_src, os.path.join(app_dir, "templates"), ignore=_copy_ignore)
        else:
            os.makedirs(os.path.join(app_dir, "templates"), exist_ok=True)

        for name in ("Dockerfile", "pyproject.toml", "uv.lock"):
            shutil.copy2(os.path.join(self.repo, name), os.path.join(app_dir, name))

        write_text(
            os.path.join(app_dir, ".dockerignore"),
            str(self.config.get("dockerignore") or DEFAULT_DOCKERIGNORE),
        )

    def _build_observability(self) -> None:
        if not self.config.get("include_observability"):
            return
        obs_dest = os.path.join(self.staging_dir, "observability")
        shutil.copytree(
            os.path.join(self.repo, str(self.config["observability_source"])),
            obs_dest,
            ignore=_copy_ignore_observability,
        )
        # Grafana mounts ./observability/provisioning; some source trees only
        # keep it under grafana/provisioning.
        root_prov = os.path.join(obs_dest, "provisioning")
        grafana_prov = os.path.join(obs_dest, "grafana", "provisioning")
        if os.path.isdir(grafana_prov) and not os.path.exists(root_prov):
            shutil.copytree(grafana_prov, root_prov, ignore=_copy_ignore)

    def _build_secrets(self) -> None:
        secrets_dir = os.path.join(self.staging_dir, "secrets")
        os.makedirs(secrets_dir, exist_ok=True)
        if self.secret_embedded:
            shutil.copy2(str(self.config["secret_path"]), os.path.join(secrets_dir, self.secret_name))
            return
        write_text(
            os.path.join(secrets_dir, "README.md"),
            "# Credentials\n\n"
            "This bundle does not embed credentials.\n\n"
            f"Place the service account file here as `{self.secret_name}` before running\n"
            "`./deploy.sh`. `docker-compose.yml` mounts it at `/run/secrets/`.\n",
        )

    def _env_overrides(self) -> dict[str, str]:
        """Bundle-critical env values, all literal."""
        models = self.config["models"]
        locations = self.config["locations"]
        provider = models.get("provider", "vertex_ai")
        overrides: dict[str, str] = {
            "PORT": str(self.config["port"]),
            "HOST_PORT": str(self.config["port"]),
            "LOG_LEVEL": str(self.config["log_level"]),
            "LITELLM_PROVIDER": provider,
            "LITELLM_DEFAULT_MODEL": qualify_model(models.get("primary", ""), provider),
            "LITELLM_REASONING_MODEL": (
                qualify_model(models.get("reasoning", ""), provider)
                or qualify_model(models.get("primary", ""), provider)
            ),
            "LITELLM_FALLBACK_MODEL": ",".join(
                qualify_model(m, provider) for m in (models.get("fallbacks") or []) if m
            ),
            "LITELLM_VISION_MODEL": (
                qualify_model(models.get("vision", ""), provider)
                or qualify_model(models.get("primary", ""), provider)
            ),
            "LITELLM_VISION_FALLBACK_MODEL": ",".join(
                qualify_model(m, provider) for m in (models.get("vision_fallbacks") or []) if m
            ),
            "DEFAULT_INFERENCE_REGION": str(locations.get("text", "")),
            "VERTEXAI_LOCATION": str(locations.get("text", "")),
            "GEMINI_LIVE_LOCATION": str(locations.get("live", "")),
            "GEMINI_FALLBACK_THINKING_LEVEL": str(models.get("fallback_thinking_level", "LOW")),
            "ENABLE_IMPROVE_SECOND_PASS": (
                "true" if self.config["features"].get("improve_second_pass") else "false"
            ),
            "ENABLE_CONSOLIDATED_ANALYZE": (
                "true" if self.config["features"].get("consolidated_analyze") else "false"
            ),
            "CORS_ORIGINS": ",".join(self.config.get("cors_origins") or []),
        }
        live = qualify_model(models.get("live", ""), provider)
        if live:
            overrides["GEMINI_LIVE_MODEL"] = live
        if locations.get("conversation"):
            overrides["CONVERSATION_VERTEXAI_LOCATION"] = str(locations["conversation"])
        return overrides

    def _build_env(self) -> dict[str, str]:
        """Write .env and .env.example; return the resolved env values."""
        source = os.path.join(self.repo, str(self.config["env_file"]))
        pairs = read_env_file(source)
        overrides = self._env_overrides()

        out_lines: list[str] = []
        handled: set[str] = set()
        for key, line in pairs:
            if key is not None and key in overrides:
                out_lines.append(f"{key}={overrides[key]}")
                handled.add(key)
            else:
                out_lines.append(line)

        appended = [f"{k}={v}" for k, v in overrides.items() if k not in handled]
        if appended:
            if out_lines and out_lines[-1].strip():
                out_lines.append("")
            out_lines.append("# --- bundle packager (literal, bundle-critical) ---")
            out_lines.extend(appended)

        env_path = os.path.join(self.staging_dir, ".env")
        write_text(env_path, "\n".join(out_lines).rstrip("\n") + "\n")

        # .env.example: same content with credential values redacted.
        example_lines: list[str] = []
        for line in out_lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if is_secret_key(key):
                    example_lines.append(f"{key}=your_{key.lower()}_here")
                    continue
            example_lines.append(line)
        write_text(
            os.path.join(self.staging_dir, ".env.example"),
            "\n".join(example_lines).rstrip("\n") + "\n",
        )

        resolved = env_lookup(read_env_file(env_path))
        return resolved

    def _build_compose(self, env_values: dict[str, str]) -> None:
        template = self.config.get("compose_template") or "builtin"
        if template == "builtin":
            content = render_compose(self.config, env_values)
        else:
            with open(template, "r", encoding="utf-8", errors="replace") as handle:
                content = apply_compose_placeholders(handle.read(), self.config)
            leftover = [token for token in ("{{", "}}") if token in content]
            if leftover:
                raise PackagingError(
                    f"compose template '{template}' has unresolved placeholders after rendering"
                )
        write_text(os.path.join(self.staging_dir, "docker-compose.yml"), content)

    def _build_scripts(self) -> None:
        for name, (content, newline) in render_scripts(self.config).items():
            path = os.path.join(self.staging_dir, name)
            write_text(path, content, newline)
            if name.endswith(".sh"):
                # Preserve the executable bit on POSIX; harmless on Windows.
                try:
                    os.chmod(path, 0o755)
                except OSError:
                    pass

    def _build_docs(self) -> None:
        write_text(os.path.join(self.staging_dir, "VERSION.txt"), render_version_txt(self.config))
        file_count = len(self._staged_files(exclude_sums=True))
        write_text(
            os.path.join(self.staging_dir, "RELEASE_MANIFEST.md"),
            render_release_manifest(self.config, self.repo, file_count),
        )
        write_text(os.path.join(self.staging_dir, "SECURITY.md"), render_security_md(self.config))

    # -- integrity ---------------------------------------------------------

    def _staged_files(self, exclude_sums: bool = True) -> list[str]:
        """Sorted relative posix paths of staged files."""
        paths = [
            rel for rel, is_dir in iter_tree(self.staging_dir) if not is_dir
        ]
        if exclude_sums:
            paths = [p for p in paths if p != "SHA256SUMS"]
        return sorted(paths)

    def _write_sums(self) -> int:
        """Write SHA256SUMS over every staged file except SHA256SUMS itself."""
        lines = []
        for rel in self._staged_files(exclude_sums=True):
            full = os.path.join(self.staging_dir, rel.replace("/", os.sep))
            lines.append(f"{sha256_file(full)}  {rel}\n")
        write_text(os.path.join(self.staging_dir, "SHA256SUMS"), "".join(sorted(lines)))
        return len(lines)

    def _create_archive(self) -> None:
        """Create a byte-reproducible .tar.gz of the staging directory."""
        os.makedirs(os.path.dirname(self.archive_path), exist_ok=True)
        with open(self.archive_path, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
                    for rel, is_dir in iter_tree(self.staging_dir):
                        full = os.path.join(self.staging_dir, rel.replace("/", os.sep))
                        info = tar.gettarinfo(full, arcname=f"{self.bundle_name}/{rel}")
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = "root"
                        info.gname = "root"
                        info.mode = (
                            0o755 if (is_dir or rel.endswith((".sh", ".cmd"))) else 0o644
                        )
                        if is_dir:
                            tar.addfile(info)
                        else:
                            with open(full, "rb") as handle:
                                tar.addfile(info, handle)

    # -- entry point -------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """Build the bundle. Order matters: files, then sums, then archive."""
        if self.dry_run:
            return {
                "dry_run": True,
                "bundle": self.bundle_name,
                "staging_dir": self.staging_dir,
                "archive": self.archive_path,
                "plan": self.plan(),
            }

        if os.path.abspath(self.out_dir) == self.repo:
            raise PackagingError(
                f"out_dir must not be the repo root ({self.repo}); it would be mutated during staging"
            )

        # Idempotent: replace the staging dir and the artifact.
        if os.path.isdir(self.staging_dir):
            shutil.rmtree(self.staging_dir)
        os.makedirs(self.staging_dir, exist_ok=True)
        if os.path.exists(self.archive_path):
            os.remove(self.archive_path)

        self._build_application()
        self._build_observability()
        self._build_secrets()
        env_values = self._build_env()
        self._build_compose(env_values)
        self._build_scripts()
        self._build_docs()

        # DEFECT 2: sums after every file exists, archive after sums.
        summed = self._write_sums()
        self._create_archive()

        size_bytes = os.path.getsize(self.archive_path)
        return {
            "dry_run": False,
            "bundle": self.bundle_name,
            "staging_dir": self.staging_dir,
            "archive": self.archive_path,
            "size_bytes": size_bytes,
            "size_kb": round(size_bytes / 1024.0, 1),
            "sha256": sha256_file(self.archive_path),
            "files": summed,
            "models": self.config["models"],
            "locations": self.config["locations"],
        }


# ---------------------------------------------------------------------------
# Config assembly
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `overlay` onto a copy of `base`."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def build_config(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Merge defaults <- JSON config <- CLI flags. Returns (config, repo)."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy

    if args.config:
        if not os.path.isfile(args.config):
            raise PackagingError(f"config file not found: {args.config}")
        with open(args.config, "r", encoding="utf-8") as handle:
            overlay = json.load(handle)
        if not isinstance(overlay, dict):
            raise PackagingError("config file must contain a JSON object")
        config = _deep_merge(config, overlay)

    repo = os.path.abspath(args.repo or os.getcwd())

    overrides = {
        "name": args.name,
        "version": args.version,
        "image_tag": args.image_tag,
        "out_dir": args.out_dir,
        "python_source": args.python_source,
        "include_observability": args.include_observability,
        "secret_path": args.embed_secrets,
        "env_file": args.env_file,
        "compose_template": args.compose_template,
        "extract_dir": args.extract_dir,
        "port": args.port,
        "container_name": args.container_name,
        "project_name": args.project_name,
    }
    for key, value in overrides.items():
        if value is not None:
            config[key] = value

    # Generic tool: a relative secret path belongs to the target repo, not to cwd.
    secret = str(config.get("secret_path") or "").strip()
    if secret and not os.path.isabs(secret):
        config["secret_path"] = os.path.normpath(os.path.join(repo, secret))

    if args.location:
        config["locations"]["text"] = args.location
    if args.live_location:
        config["locations"]["live"] = args.live_location
    if args.model:
        config["models"]["primary"] = args.model
    if args.live_model:
        config["models"]["live"] = args.live_model
    if args.cors_origin:
        config["cors_origins"] = list(args.cors_origin)

    if not config.get("name"):
        config["name"] = os.path.basename(repo.rstrip(os.sep))

    # Relative out_dir resolves against the repo so runs are location-stable.
    if not os.path.isabs(str(config["out_dir"])):
        config["out_dir"] = os.path.join(repo, str(config["out_dir"]))

    return config, repo


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

_FIXTURE_OBSERVABILITY = {
    "loki/loki-config.yaml": "auth_enabled: false\n",
    "alloy/config.alloy": "// fixture alloy config\n",
    "grafana/provisioning/datasources/loki.yaml": "apiVersion: 1\n",
    "README.md": "ignored on purpose\n",
}


def _build_fixture(root: str) -> None:
    """Create a tiny repo that satisfies every required source path."""
    os.makedirs(os.path.join(root, "src"), exist_ok=True)
    write_text(os.path.join(root, "src", "__init__.py"), "")
    write_text(os.path.join(root, "src", "main.py"), "app = None\n")
    os.makedirs(os.path.join(root, "templates"), exist_ok=True)
    write_text(os.path.join(root, "templates", "index.html"), "<html></html>\n")
    write_text(os.path.join(root, "Dockerfile"), "FROM python:3.12-slim\n")
    write_text(os.path.join(root, "pyproject.toml"), "[project]\nname = \"fixture\"\n")
    write_text(os.path.join(root, "uv.lock"), "version = 1\n")
    write_text(
        os.path.join(root, ".env"),
        "# fixture env\n"
        "ENVIRONMENT=development\n"
        "LITELLM_DEFAULT_MODEL=vertex_ai/some-stale-model\n"
        "VERTEXAI_LOCATION=global\n"
        "INTELLIGENCE_API_KEY=fixture-key-value\n"
        "SARVAM_API_KEY=fixture-sarvam-value\n"
        "VERTEX_PROJECT=fixture-project\n",
    )
    os.makedirs(os.path.join(root, "secrets"), exist_ok=True)
    write_text(os.path.join(root, "secrets", "gemini-sa.json"), '{"type": "service_account"}\n')
    obs_root = os.path.join(root, "deployment", "observability")
    for rel, content in _FIXTURE_OBSERVABILITY.items():
        write_text(os.path.join(obs_root, rel.replace("/", os.sep)), content)


def _verify_sums(staging: str) -> int:
    """Recompute every hash in SHA256SUMS; raise AssertionError on mismatch."""
    sums_path = os.path.join(staging, "SHA256SUMS")
    assert os.path.isfile(sums_path), "SHA256SUMS missing"
    with open(sums_path, "r", encoding="utf-8") as handle:
        lines = [line.rstrip("\n") for line in handle if line.strip()]
    assert lines, "SHA256SUMS is empty"
    assert lines == sorted(lines), "SHA256SUMS is not sorted"
    listed = set()
    for line in lines:
        digest, rel = line.split("  ", 1)
        listed.add(rel)
        full = os.path.join(staging, rel.replace("/", os.sep))
        assert os.path.isfile(full), f"SHA256SUMS lists a missing file: {rel}"
        actual = sha256_file(full)
        assert actual == digest, f"checksum mismatch for {rel}: {digest} != {actual}"
    assert "SHA256SUMS" not in listed, "SHA256SUMS must not list itself"
    on_disk = {
        rel for rel, is_dir in iter_tree(staging) if not is_dir and rel != "SHA256SUMS"
    }
    assert on_disk == listed, f"SHA256SUMS does not cover every file: {on_disk ^ listed}"
    return len(lines)


def _selftest_config(fixture: str, out_dir: str, **overrides: Any) -> dict[str, Any]:
    config, _repo = build_config(
        argparse.Namespace(
            config=None,
            repo=fixture,
            name="fixture-bundle",
            version="v0.0.1-selftest",
            image_tag="fixture/image:v0.0.1-selftest",
            out_dir=out_dir,
            python_source=None,
            include_observability=None,
            embed_secrets=os.path.join(fixture, "secrets", "gemini-sa.json"),
            env_file=None,
            compose_template=None,
            extract_dir=None,
            port=None,
            container_name=None,
            project_name=None,
            location=None,
            live_location=None,
            model=None,
            live_model=None,
            cors_origin=None,
        )
    )
    config.update(overrides)
    return config


def run_self_test() -> int:
    """Assert the layout, the sums-last ordering, and literal compose values."""
    checks = 0

    def ok(message: str) -> None:
        nonlocal checks
        checks += 1
        print(f"  [ok] {message}")

    with tempfile.TemporaryDirectory(prefix="bundle-selftest-") as tmp:
        fixture = os.path.join(tmp, "fixture-repo")
        out_dir = os.path.join(tmp, "out")
        os.makedirs(fixture, exist_ok=True)
        _build_fixture(fixture)

        config = _selftest_config(fixture, out_dir)
        errors = validate_model_matrix(config["models"], config["locations"])
        assert not errors, f"default matrix should validate, got: {errors}"
        ok("default model matrix validates")

        # Defect 5: the recorded (model, location) failures must be rejected.
        bad_models = json.loads(json.dumps(config["models"]))
        bad_models["primary"] = "gemini-3.8-flash"
        bad = validate_model_matrix(bad_models, config["locations"])
        assert bad and "gemini-3.8-flash" in bad[0], "gemini-3.8-flash in asia-south1 must be rejected"
        ok("known-invalid text model rejected (gemini-3.8-flash @ asia-south1)")

        bad_live = json.loads(json.dumps(config["models"]))
        bad_live["live"] = "gemini-live-2.5-flash-native-audio"
        bad = validate_model_matrix(bad_live, {"text": "asia-south1", "live": "asia-south1"})
        assert bad and "gemini-live" in bad[0], "Gemini Live in asia-south1 must be rejected"
        ok("known-invalid live model rejected (gemini-live-2.5-flash-native-audio @ asia-south1)")

        good_live = validate_model_matrix(
            bad_live, {"text": "asia-south1", "live": "us-central1"}
        )
        assert not good_live, f"live model should be valid in us-central1: {good_live}"
        ok("live model accepted in us-central1 (locations are independent)")

        # Required-input validation must fail fast.
        for key, needle in (("version", "version"), ("image_tag", "image_tag")):
            broken = json.loads(json.dumps(config))
            broken[key] = ""
            try:
                validate_config(broken, fixture)
            except PackagingError as exc:
                assert needle in str(exc), f"error should name {needle}: {exc}"
            else:
                raise AssertionError(f"missing {key} should fail fast")
        ok("missing required inputs fail fast")

        try:
            validate_config(dict(config, python_source="does-not-exist"), fixture)
        except PackagingError as exc:
            assert "python source" in str(exc), exc
        else:
            raise AssertionError("missing source path should fail fast")
        ok("missing source path fails fast")

        # Packaging.
        packager = BundlePackager(config, fixture)
        result = packager.run()
        staging = result["staging_dir"]
        assert not result["dry_run"]
        ok("packaging completed")

        # Layout.
        expected = [
            "application/src/main.py",
            "application/templates/index.html",
            "application/Dockerfile",
            "application/pyproject.toml",
            "application/uv.lock",
            "application/.dockerignore",
            "observability/loki/loki-config.yaml",
            "observability/alloy/config.alloy",
            "observability/provisioning/datasources/loki.yaml",
            "secrets/gemini-sa.json",
            ".env",
            ".env.example",
            "docker-compose.yml",
            "deploy.sh",
            "deploy.ps1",
            "launcher.sh",
            "verify.sh",
            "verify.ps1",
            "VERSION.txt",
            "RELEASE_MANIFEST.md",
            "SECURITY.md",
            "SHA256SUMS",
        ]
        for rel in expected:
            assert os.path.isfile(os.path.join(staging, rel.replace("/", os.sep))), f"missing {rel}"
        ok(f"layout complete ({len(expected)} required entries)")

        for absent in ("observability/README.md", "application/src/__pycache__"):
            assert not os.path.exists(os.path.join(staging, absent.replace("/", os.sep))), (
                f"should have been excluded: {absent}"
            )
        ok("observability README and caches excluded")

        # DEFECT 2: SHA256SUMS written last and verifies.
        count = _verify_sums(staging)
        ok(f"SHA256SUMS verifies over all {count} files and excludes itself")

        version_txt = os.path.join(staging, "VERSION.txt")
        sums_text = read_text(os.path.join(staging, "SHA256SUMS"))
        assert "docker-compose.yml" in sums_text, "sums must cover compose"
        assert "VERSION.txt" in sums_text, "sums must cover VERSION.txt"
        compose_text = read_text(os.path.join(staging, "docker-compose.yml"))
        assert f"{sha256_file(os.path.join(staging, 'docker-compose.yml'))}  docker-compose.yml" in sums_text
        assert f"{sha256_file(version_txt)}  VERSION.txt" in sums_text
        ok("sums were generated after compose and VERSION.txt (ordering proven)")

        # DEFECT 1: literal model/location values, no interpolation for them.
        primary = qualify_model(config["models"]["primary"], config["models"]["provider"])
        assert f"- LITELLM_DEFAULT_MODEL={primary}\n" in compose_text, (
            "compose must carry the literal primary model"
        )
        text_location = config["locations"]["text"]
        assert f"- VERTEXAI_LOCATION={text_location}\n" in compose_text
        assert f"- GEMINI_LIVE_LOCATION={config['locations']['live']}\n" in compose_text
        assert "CORS_ORIGINS=" in compose_text
        for key in (
            "LITELLM_DEFAULT_MODEL",
            "LITELLM_FALLBACK_MODEL",
            "LITELLM_VISION_MODEL",
            "LITELLM_VISION_FALLBACK_MODEL",
            "LITELLM_REASONING_MODEL",
            "VERTEXAI_LOCATION",
            "DEFAULT_INFERENCE_REGION",
            "GEMINI_LIVE_LOCATION",
            "GEMINI_LIVE_MODEL",
        ):
            for needle in (f"${{{key}", f"${{{key}:"):
                assert needle not in compose_text, f"{key} must not be interpolated in compose"
        assert "- INTELLIGENCE_API_KEY=${INTELLIGENCE_API_KEY}\n" in compose_text
        assert "fixture-key-value" not in compose_text, "secrets must not be inlined into compose"
        ok("compose has literal model/location values; only the secret is interpolated")

        # .env resolved the overrides, .env.example redacted the secrets.
        bundle_env = read_text(os.path.join(staging, ".env"))
        assert f"LITELLM_DEFAULT_MODEL={primary}" in bundle_env, "compose/.env model must agree"
        assert "vertex_ai/some-stale-model" not in bundle_env, "stale .env model must be replaced"
        example = read_text(os.path.join(staging, ".env.example"))
        assert "fixture-key-value" not in example, ".env.example must redact the service key"
        assert "fixture-sarvam-value" not in example, ".env.example must redact provider keys"
        assert "INTELLIGENCE_API_KEY=your_" in example
        ok(".env overrides applied and .env.example redacts credentials")

        # VERSION.txt carries the required facts.
        version_body = read_text(version_txt)
        for needle in (
            "Bundle Version:",
            "Target Port:",
            "Model Matrix:",
            "Primary:",
            "Vision:",
            "Live:",
            "Locations:",
            "Endpoints:",
            "/api/v1/resume/analyze",
            "/api/v1/job/match",
            "/api/v1/job/optimize",
            "/api/v1/conversation/session/scratch",
            "/api/v1/conversation/ticket",
            "/api/v1/conversation/ws/",
            "Exclusions:",
            "Credential Model:",
        ):
            assert needle in version_body, f"VERSION.txt missing {needle!r}"
        ok("VERSION.txt has version, port, model matrix, endpoints, exclusions, credentials")

        # Determinism: no BOM, LF newlines, byte-identical re-run.
        compose_bytes = open(os.path.join(staging, "docker-compose.yml"), "rb").read()
        assert not compose_bytes.startswith(b"\xef\xbb\xbf"), "compose must be UTF-8 without BOM"
        assert b"\r\n" not in compose_bytes, "compose must use LF newlines"
        assert open(version_txt, "rb").read()[:3] != b"\xef\xbb\xbf"
        ok("generated files are UTF-8 without BOM and LF-terminated")

        # Archive.
        assert os.path.isfile(result["archive"]), "tar.gz missing"
        assert result["size_kb"] > 0, "size_kb must be reported"
        assert len(result["sha256"]) == 64, "archive sha256 must be reported"
        ok(f"archive created: {result['size_kb']} KB, sha256 {result['sha256'][:16]}...")

        with tarfile.open(result["archive"], "r:gz") as tar:
            names = tar.getnames()
        assert f"fixture-bundle/docker-compose.yml" in names
        assert f"fixture-bundle/SHA256SUMS" in names
        ok("archive contains the bundle root directory")

        # Idempotent + reproducible.
        second = BundlePackager(config, fixture).run()
        assert second["sha256"] == result["sha256"], "re-run must produce a byte-identical archive"
        ok("re-run is idempotent and byte-reproducible")

        # Dry run writes nothing.
        dry_out = os.path.join(tmp, "dry-out")
        dry = BundlePackager(_selftest_config(fixture, dry_out), fixture, dry_run=True).run()
        assert dry["dry_run"] and not os.path.exists(dry_out), "dry run must not write"
        assert dry["plan"], "dry run must report a plan"
        ok("dry run reports a plan without writing")

        # Custom compose template that omits the image tag must be rejected.
        template = os.path.join(tmp, "compose.tpl.yml")
        write_text(
            template,
            "services:\n  app:\n    image: some/other-image:latest\n",
        )
        try:
            validate_compose_template(template, config["image_tag"])
        except PackagingError as exc:
            assert "image tag" in str(exc), exc
        else:
            raise AssertionError("template without the configured image tag must be rejected")
        ok("compose template referencing an unset image tag is rejected")

        # A template carrying the placeholder renders cleanly.
        write_text(
            template,
            "services:\n  app:\n    image: {{IMAGE_TAG}}\n    ports:\n      - \"{{PORT}}:{{PORT}}\"\n",
        )
        validate_compose_template(template, config["image_tag"])
        custom = _selftest_config(fixture, os.path.join(tmp, "custom-out"))
        custom["compose_template"] = template
        custom_result = BundlePackager(custom, fixture).run()
        rendered = read_text(os.path.join(custom_result["staging_dir"], "docker-compose.yml"))
        assert config["image_tag"] in rendered and "{{" not in rendered
        ok("custom compose template placeholders substitute cleanly")

    print(f"\nSELF-TEST PASSED ({checks} checks)")
    return checks


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bundlewright",
        description="Package a service repo into a turnkey, self-verifying deployment bundle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m bundlewright --self-test\n"
            "  python -m bundlewright --repo . --version v1.0.0 --image-tag app:v1.0.0\n"
            "  python -m bundlewright --config bundle.json --dry-run\n"
        ),
    )
    parser.add_argument("--repo", default=None, help="source repository root (default: cwd)")
    parser.add_argument("--config", default=None, help="JSON config file merged over the defaults")
    parser.add_argument("--name", default=None, help="bundle name (default: repo directory name)")
    parser.add_argument("--version", default=None, help="bundle version, e.g. v1.0.0")
    parser.add_argument("--image-tag", dest="image_tag", default=None, help="container image tag")
    parser.add_argument("--out-dir", dest="out_dir", default=None, help="output directory for the staging dir and .tar.gz")
    parser.add_argument("--python-source", dest="python_source", default=None, help="python source directory (default: src)")
    parser.add_argument(
        "--include-observability",
        dest="include_observability",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="include the loki/alloy/grafana stack (default: include)",
    )
    parser.add_argument(
        "--embed-secrets",
        dest="embed_secrets",
        default=None,
        metavar="PATH",
        help="credential file to embed under secrets/",
    )
    parser.add_argument("--env-file", dest="env_file", default=None, help="env file to use as the bundle base (default: .env)")
    parser.add_argument(
        "--compose-template",
        dest="compose_template",
        default=None,
        metavar="PATH|builtin",
        help="custom compose template path, or 'builtin'",
    )
    parser.add_argument(
        "--extract-dir",
        dest="extract_dir",
        default=None,
        metavar="PATH",
        help="explicit temp dir for extraction outside the workspace (no hardcoded default)",
    )
    parser.add_argument("--port", type=int, default=None, help="container/service port (default: 7500)")
    parser.add_argument("--project-name", dest="project_name", default=None, help="compose project name (default: bundle name)")
    parser.add_argument(
        "--container-name",
        dest="container_name",
        default=None,
        help="fixed container_name; {project} is substituted; '' omits it for side-by-side runs",
    )
    parser.add_argument("--location", default=None, help="text/vision Vertex location, literal in compose")
    parser.add_argument("--live-location", dest="live_location", default=None, help="Gemini Live location, literal in compose")
    parser.add_argument("--model", default=None, help="primary model name or fully-qualified route")
    parser.add_argument("--live-model", dest="live_model", default=None, help="Gemini Live model name")
    parser.add_argument(
        "--cors-origin",
        dest="cors_origin",
        action="append",
        default=None,
        metavar="ORIGIN",
        help="allowed browser origin; repeatable, joins as CORS_ORIGINS",
    )
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="print the plan and write nothing")
    parser.add_argument(
        "--allow-unverified-models",
        dest="allow_unverified_models",
        action="store_true",
        help="skip the KNOWN_INVALID model/location check",
    )
    parser.add_argument("--self-test", dest="self_test", action="store_true", help="run the built-in self test")
    parser.add_argument(
        "--tool-version",
        dest="tool_version",
        action="store_true",
        help="print bundlewright's own version and exit (--version is the bundle version)",
    )
    parser.add_argument("--json", dest="json_out", action="store_true", help="print the result as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if getattr(args, "tool_version", False):
        try:
            from . import __version__ as tool_version
        except ImportError:  # executed as a plain script
            tool_version = "1.0.0"
        print(f"bundlewright {tool_version}")
        return 0

    if args.self_test:
        return 0 if run_self_test() else 1

    try:
        config, repo = build_config(args)

        problems = validate_model_matrix(config["models"], config["locations"])
        if problems and not args.allow_unverified_models:
            raise PackagingError(
                "known-invalid model/location pair(s):\n  - "
                + "\n  - ".join(problems)
                + "\nRe-run with --allow-unverified-models to bypass (only if you have "
                "verified availability yourself)."
            )
        if problems:
            for problem in problems:
                print(f"WARNING (allowed): {problem}", file=sys.stderr)

        validate_config(config, repo)

        if config.get("extract_dir"):
            config["_extract_dir"] = os.path.abspath(str(config["extract_dir"]))

        result = BundlePackager(config, repo, dry_run=args.dry_run).run()

        if args.json_out:
            print(json.dumps(result, indent=2, default=str))
            return 0

        if result["dry_run"]:
            print(f"DRY RUN - {result['bundle']}")
            print(f"  staging dir : {result['staging_dir']}")
            print(f"  archive     : {result['archive']}")
            print("  planned steps:")
            for step in result["plan"]:
                print(f"    - {step}")
            return 0

        print("=== PACKAGING COMPLETE ===")
        print(f"bundle      : {result['bundle']}")
        print(f"staging_dir : {result['staging_dir']}")
        print(f"archive     : {result['archive']}")
        print(f"size_kb     : {result['size_kb']}")
        print(f"sha256      : {result['sha256']}")
        print(f"files       : {result['files']} (excluding SHA256SUMS)")
        return 0

    except PackagingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
