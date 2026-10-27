# Changelog

## 0.1.2rc1 — 2026-10-28

- Move SDK development to the public `tazolabs/tazo-sdk` repository, with
  standalone tests, documentation sources, and release workflows.
- Add public source and issue-tracker links to the distribution metadata.
- Prepare a release candidate to verify TestPyPI and PyPI trusted publishing
  from the public repository.
- Preserve the 0.1.1 runtime behavior; only the runtime version identifier changes.

## 0.1.1 — 2026-10-27

- Make PyPI installation the primary path in the README and customer guides.
- Correct publication status and remove release-candidate wording from the reference.
- Clarify tag deployment rules and live API smoke testing in the release guide.
- SDK request and response behavior is unchanged.

## 0.1.0 — 2026-10-27

- Typed sync and async clients for decisions, streaming, models, and health.
- Replayable uploads with per-operation idempotency and bounded retries.
- Typed WebSocket events, explicit turn lifecycle, and PCM16 WAV chunks.
- Public-only Mintlify guides, tested endpoint samples, and Sphinx reference.

Supports Tazo API v1. No internal endpoints are part of the SDK.
