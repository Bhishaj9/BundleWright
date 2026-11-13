# Security

## Supported versions

| Version | Supported |
|---|---|
| 1.x | yes |

## Reporting a vulnerability

Do not open a public issue for a vulnerability. Use GitHub's private
vulnerability reporting on this repository (Security → Report a vulnerability),
or contact the maintainer directly. Include the version, the command, and a
minimal reproduction. You will get an acknowledgement, then an assessment and a
fix or mitigation.

## What this tool touches

`bundlewright` reads a repository and writes a bundle directory and archive. It
does not contact the network, and it does not execute the packaged service.

## Credentials — read this before sharing a bundle

A bundle can embed credential files on request (`--embed-secrets`) and always
copies the target repo's `.env` into the bundle. That is intentional for sealed
internal handoffs, and it means:

- **Never commit a produced `.tar.gz` or an embedded `.env` to a repository.**
  The `.gitignore` here ignores both.
- Transfer bundles over a private channel and send the SHA-256 alongside.
- Verify before deploying: `sha256sum -c <bundle>.tar.gz.sha256`.
- If a bundle leaks, rotate the credentials it carried. The bundle does not
  record which secrets it contains beyond the filename.

## What the tool refuses to do

- Emit a model/location pair it knows is invalid (unless explicitly overridden
  with `--allow-unverified-models`).
- Produce an archive whose `SHA256SUMS` does not verify (sums are generated
  last, over the final tree).
- Silently drop executable bits on launchers.
