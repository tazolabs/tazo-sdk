# SDK synchronization

Automated SDK updates arrive in a pull request from `codex/sdk-sync` with the
`sdk-sync` and `release:patch` labels. The automation refreshes the branch
from the latest SDK `main` when exported files change. Keep unrelated work on
a separate branch.

1. Review the SDK implementation, public API contracts, examples, and docs in
   the diff. The installed-wheel CI matrix runs on the pull request.
2. Confirm the version bump is appropriate and finalize the release notes
   before merging. The automated update prepares a patch version by default.
3. After merge and green CI on `main`, follow [RELEASING.md](RELEASING.md) to
   tag the reviewed commit, verify the package on TestPyPI, and publish to PyPI.

The `.sdk-sync.json` manifest lists the files managed by automation and their
SHA-256 hashes. It lets the exporter identify which files to remove when they
are dropped from the export.
