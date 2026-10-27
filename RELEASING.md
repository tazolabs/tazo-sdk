# Release a new SDK version

The imported SDK is already available as `tazo-sdk==0.1.1` on PyPI and TestPyPI.
Do not upload 0.1.1 again. Complete the one-time environments and trusted
publisher setup in [SETUP.md](SETUP.md) before publishing a **new** version from
`tazolabs/tazo-sdk`.

The commands below use **0.1.2** as the first future release example. Do not run
them until the version change is reviewed and merged. Publishing workflows are
manual; merging this migration PR does not publish a package.

## Prepare and review

1. Update `pyproject.toml` and `src/tazo/_version.py` to the same new version.
   Finalize `CHANGELOG.md` and the customer changelog in
   `docs/mintlify/changelog.mdx`; update current-version wording in the README.
   Keep `compatibility/published-0.1.1.json` as historical provenance.
2. Run `uv lock` and commit the updated lockfile. The SDK remains an independent
   package with only HTTPX, Pydantic, and websockets as runtime dependencies.
3. Run the local checks, build, and metadata validation:

   ```sh
   uv sync --frozen --all-groups
   uv run --frozen pytest
   uv run --frozen ruff check .
   uv run --frozen ruff format --check .
   uv run --frozen mypy src/tazo examples
   uv build --no-sources
   uv run --frozen --group release python -m twine check dist/*
   ```

4. Install Node.js 22 or 24 LTS and run `npm ci`. Regenerate and check docs:

   ```sh
   uv run --frozen --group docs python scripts/sdk_reference.py
   uv run --frozen --group docs python scripts/sdk_reference.py --check
   uv run --frozen --group docs sphinx-build -W --keep-going -b html docs/source docs/_build/html
   ```

5. With a dedicated Tazo key set as `TAZO_API_KEY`, run live verification:

   ```sh
   uv run --frozen python examples/verify.py --audio examples/weather.wav \
     --expected-choice get_weather --output ./tazo-sdk-verification.json
   uv run --frozen python examples/smoke.py \
     --base-url https://api.tazolabs.com --audio examples/weather.wav
   ```

   The first command checks all four client modes for both models. The second
   also checks that replaying an upload with its retained idempotency key
   preserves the operation. Both submit real inference. TestPyPI does not
   create a separate API deployment; use a custom origin only if one has been
   deployed and you have a key valid for it. Live tests never run automatically
   in CI or the release workflow.
6. Open a PR, review the package and docs, and merge after CI succeeds. CI tests
   installed wheels on Python 3.11–3.14 on Linux plus Python 3.13 on macOS and
   Windows, and checks the generated reference. Sync customer docs to Mintlify
   through its normal review/deployment process.

## Tag the reviewed main commit

Once the new version is merged and CI is green on `main`:

```sh
git switch main
git pull --ff-only origin main
git tag sdk-v0.1.2
git push origin sdk-v0.1.2
```

The release guard requires matching manifest/runtime versions, a matching
`sdk-v<version>` tag, and a commit reachable from `main`. It deliberately rejects
already-published 0.1.0 and 0.1.1. Do not move a tag after publication.

## Publish to TestPyPI first

The workflow must be merged into the default branch before it appears under
Actions. Dispatch **Release Python SDK** from the new tag with target `testpypi`:

```sh
gh workflow run sdk-release.yml --repo tazolabs/tazo-sdk \
  --ref sdk-v0.1.2 -f target=testpypi
```

Inspect the run in the repository's Actions tab. The build job tests the wheel,
checks metadata, and uploads distributions; the publish job uses those exact
files. The environment must allow **Tag** `sdk-v*`, not a Branch rule.

Install the new TestPyPI version in a clean Python 3.11+ environment. Use the
same dependency ranges from `pyproject.toml`; for this example:

```sh
python3 -m venv /tmp/tazo-sdk-testpypi-0.1.2
source /tmp/tazo-sdk-testpypi-0.1.2/bin/activate
env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL PIP_CONFIG_FILE=/dev/null \
  python -m pip install --index-url https://pypi.org/simple/ \
  'httpx>=0.27,<1' 'pydantic>=2.10,<3' 'websockets>=15,<18'
env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL PIP_CONFIG_FILE=/dev/null \
  python -m pip install --index-url https://test.pypi.org/simple/ \
  --no-deps 'tazo-sdk==0.1.2'
python -m pip check
python -c 'import tazo; print(tazo.__version__, tazo.__file__)'
python examples/verify.py --audio examples/weather.wav \
  --expected-choice get_weather --output /tmp/tazo-sdk-testpypi-verification.json
```

Run the last command from the checkout at the release tag, using this clean
environment and with `TAZO_API_KEY` already configured. Do not use `uv run` for
this check; it would select the development environment instead of this wheel.

## Publish the same reviewed tag to PyPI

After TestPyPI installation and live checks pass:

```sh
gh workflow run sdk-release.yml --repo tazolabs/tazo-sdk \
  --ref sdk-v0.1.2 -f target=pypi
```

Approve the `pypi` deployment if a required reviewer is configured. Each dispatch
builds and tests its own distributions from the same tag, then publishes the
exact tested files from that run. Neither workflow uses `skip-existing`.

Verify normal PyPI installation in another clean environment:

```sh
python3 -m venv /tmp/tazo-sdk-pypi-0.1.2
source /tmp/tazo-sdk-pypi-0.1.2/bin/activate
env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL PIP_CONFIG_FILE=/dev/null \
  python -m pip install --index-url https://pypi.org/simple/ 'tazo-sdk==0.1.2'
python -m pip check
python -c 'import tazo; print(tazo.__version__, tazo.__file__)'
```

Check the PyPI README/source links, Mintlify SDK pages, and release notes. A
GitHub release for the tag is optional. Published package versions are immutable;
if a correction is needed, prepare another version rather than replacing files.
