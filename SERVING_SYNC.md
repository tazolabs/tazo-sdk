# Serving to SDK synchronization

SDK implementation changes originate in `tazolabs/tazo-serving`, under
`packages/tazo-sdk`. The standalone `tazolabs/tazo-sdk` repository owns package
versions, release history, and TestPyPI/PyPI publication. Both repositories keep
regular tracked files: Git does not follow a filesystem symlink into another
repository when it commits or runs GitHub Actions.

## After a serving merge

1. A push to serving's production branch, `master`, affecting SDK inputs runs
   **Sync standalone Python SDK**. It can also be dispatched manually on master.
2. `scripts/sync_sdk.py` exports tracked implementation, examples, shared tests,
   Sphinx sources, SDK guides, and public OpenAPI/AsyncAPI snapshots into an SDK
   checkout. Console-only OpenAPI routes are filtered out. SDK-only test fixtures,
   publishing workflows, release history, and source/issue metadata are preserved.
   Runtime dependency changes are carried forward. Deleted files are removed only
   if a previous `.serving-sync.json` recorded ownership of them.
3. If there are changes, the exporter prepares the next patch version from SDK
   `main`, updates the lockfile and reference, and runs SDK checks. Identical exports
   are a no-op. The serving package's development version is never copied over the
   published package version.
4. The workflow pushes `codex/serving-sync` with a repository-scoped token.
   This branch belongs to the automation: later serving changes replace it with
   the latest export from SDK main. Do not put unrelated edits on this branch.
5. The source workflow creates or updates one SDK PR with `sdk-sync` and
   `release:patch` labels, authenticated with the repository-scoped token. Its
   installed-wheel CI matrix runs on the PR. Review breaking/API additions for a different version
   bump before merging, and finalize the release notes.
6. After review and merge, follow `RELEASING.md`: create `sdk-v<version>` on the
   reviewed main commit, publish to TestPyPI, verify it, then publish to PyPI.
   A PR label is not a Git release tag. The sync never publishes packages itself.

`.serving-sync.json` records the serving commit and hashes of the exported files.
It connects a public SDK diff to its source revision without exposing private
serving source files. Generated references and the lockfile are rebuilt separately.

## Authentication and bootstrap

Deploy keys are disabled by the SDK repository's GitHub policy. Configure a
dedicated fine-grained token instead of copying a broad personal CLI credential:

1. Create a fine-grained token with resource owner `tazolabs`, repository access
   restricted to **tazo-sdk**, with **Contents: read and write** and
   **Pull requests: read and write**. Metadata read is automatic. The token does
   not need serving access, package publishing, Actions, or workflow editing.
   Use an expiry and rotate it
   before it expires; organization approval may be required.
2. Add it as **SDK_SYNC_TOKEN** in `tazolabs/tazo-serving` → Settings → Secrets
   and variables → Actions. Never commit it or paste it into a PR or chat.
   From an already-secured token file, the equivalent command is:

   ```sh
   gh secret set SDK_SYNC_TOKEN --repo tazolabs/tazo-serving < /secure/path/token
   ```

3. Ensure the labels `sdk-sync` and `release:patch` exist in the SDK repository.
   The initial setup creates them. This organization also disables PR creation
   with `GITHUB_TOKEN`; the dedicated token creates the PR as its account owner
   and respects that policy without changing it.

The source workflow only runs on master; pull requests never receive this secret.
It fails with an actionable error if the token has not been configured. A PR
created with the dedicated token triggers the SDK's normal CI.

Merge the initial SDK update, configure the secret, then merge the serving sync
workflow. For a deliberate
retry after a transient failure:

```sh
gh workflow run sdk-sync.yml --repo tazolabs/tazo-serving --ref master
```

## Local export

Keep both checkouts next to each other. From the serving root:

```sh
uv run --frozen --all-packages --all-groups python scripts/sync_sdk.py \
  --target ../tazo-sdk --bump
```

Use a clean SDK feature branch first. Inspect the diff, then run `uv lock`,
`uv sync --frozen --all-groups`, `npm ci`, and
`uv run --frozen python scripts/sdk_reference.py` from the SDK root, followed by
its test/build checks. Omitting `--bump` preserves the destination's version.
Never follow a local symlink as the source of a release.
