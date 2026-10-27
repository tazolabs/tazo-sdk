# Test the publishing workflow with 0.1.2rc1

This release candidate tests publication from `tazolabs/tazo-sdk`. It changes
the runtime version identifier and includes the public repository's metadata
and documentation. Its other runtime files are identical to stable 0.1.1.
Fresh installations without prerelease selection continue using stable 0.1.1.

Merging the preparation PR does not publish a package. Follow the steps in order
and wait for each publication to succeed before trying to install its package.

## 1. Merge the preparation PR

Merge after CI succeeds. In the public SDK checkout, update `main`:

```sh
git switch main
git pull --ff-only origin main
```

The version in both `pyproject.toml` and `src/tazo/_version.py` must be
`0.1.2rc1`. Local checks, if needed:

```sh
UV_HTTP_TIMEOUT=300 UV_CONCURRENT_DOWNLOADS=1 UV_LINK_MODE=copy \
  uv sync --frozen --all-groups
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy src/tazo examples
```

These checks use local fake servers and do not submit inference requests.

## 2. Tag the reviewed main commit

Wait for CI on `main` to pass, then create this new tag:

```sh
git tag sdk-v0.1.2rc1
git push origin sdk-v0.1.2rc1
```

The release workflow checks the matching version/tag and that the commit is
reachable from `main`. The configured environment Tag rule `sdk-v*` also matches
this candidate. Leave the tag fixed once published. If a published candidate
needs correction, prepare a new version such as `0.1.2rc2`.

## 3. Publish to TestPyPI

```sh
gh workflow run sdk-release.yml --repo tazolabs/tazo-sdk \
  --ref sdk-v0.1.2rc1 -f target=testpypi
```

Alternatively, in GitHub Actions, select **Release Python SDK → Run workflow**,
choose tag `sdk-v0.1.2rc1` as the ref and `testpypi` as the target, then run it.

Open [GitHub Actions](https://github.com/tazolabs/tazo-sdk/actions/workflows/sdk-release.yml).
Wait until both `build` and `publish` succeed. You can also list recent runs:

```sh
gh run list --repo tazolabs/tazo-sdk --workflow sdk-release.yml --limit 5
```

Check that [TestPyPI lists 0.1.2rc1](https://test.pypi.org/project/tazo-sdk/0.1.2rc1/)
before continuing. An installation error listing only 0.1.1 means the candidate
has not been published yet; inspect the workflow first.

If publication fails, compare the existing project's trusted publisher with
owner `tazolabs`, repository `tazo-sdk`, workflow filename `sdk-release.yml`,
and environment `testpypi`. The environment deployment policy must be a **Tag**
rule, not a Branch rule. See [SETUP.md](SETUP.md) for the setup checklist.

## 4. Install the exact TestPyPI candidate

Use Python 3.11+ and a separate environment. From the repository root:

```sh
python3 -m venv /tmp/tazo-sdk-rc1-testpypi
source /tmp/tazo-sdk-rc1-testpypi/bin/activate
env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL PIP_CONFIG_FILE=/dev/null \
  python -m pip install --timeout 300 --index-url https://pypi.org/simple/ \
  'httpx>=0.27,<1' 'pydantic>=2.10,<3' 'websockets>=15,<18'
env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL PIP_CONFIG_FILE=/dev/null \
  python -m pip install --timeout 300 --index-url https://test.pypi.org/simple/ \
  --no-deps --pre 'tazo-sdk==0.1.2rc1'
python -m pip check
python -c 'import tazo; print(tazo.__version__, tazo.__file__)'
```

Confirm that the version is exactly `0.1.2rc1` and the import comes from this
environment's `site-packages`. Dependencies come from normal PyPI; only the SDK
comes from TestPyPI. The command-scoped pip settings avoid unrelated private
package indexes. On Windows, create a local virtual environment and activate
`Scripts/Activate.ps1`; the `env -u` commands above are for Bash/Zsh.

## 5. Verify the installed package with the live API

Have your existing raw `TAZO_API_KEY` available in the terminal. It is the Tazo
key, separate from package-publishing authentication. From the repository root:

```sh
python examples/verify.py --audio examples/weather.wav \
  --expected-choice get_weather \
  --output /tmp/tazo-sdk-rc1-testpypi-verification.json
python examples/smoke.py --base-url https://api.tazolabs.com \
  --audio examples/weather.wav
```

Use this environment's `python`, not `uv run`, to exercise the installed wheel.
The first command must print SDK `0.1.2rc1` and `Summary: 8/8 passed`; it checks
sync and async uploads and streaming for both models. The second additionally
checks retained-key upload replay and streaming completion. Both submit real
inference work and use the account's limits. TestPyPI does not provide a separate
API deployment.

## 6. Test the PyPI publisher with the same candidate

To verify the production publisher too, publish the same reviewed candidate tag:

```sh
gh workflow run sdk-release.yml --repo tazolabs/tazo-sdk \
  --ref sdk-v0.1.2rc1 -f target=pypi
```

The GitHub Actions interface also works: choose the same tag with target `pypi`.

Approve the `pypi` environment deployment if it requires a reviewer. Wait until
`build` and `publish` succeed, then confirm the
[PyPI candidate page](https://pypi.org/project/tazo-sdk/0.1.2rc1/) exists. This
publishes a prerelease; it does not replace the stable 0.1.1 files. The PyPI
trusted publisher must use environment `pypi`.

Install it in another clean environment:

```sh
deactivate
python3 -m venv /tmp/tazo-sdk-rc1-pypi
source /tmp/tazo-sdk-rc1-pypi/bin/activate
env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL PIP_CONFIG_FILE=/dev/null \
  python -m pip install --timeout 300 --index-url https://pypi.org/simple/ \
  --pre 'tazo-sdk==0.1.2rc1'
python -m pip check
python -c 'import tazo; print(tazo.__version__, tazo.__file__)'
python examples/verify.py --audio examples/weather.wav \
  --expected-choice get_weather --output /tmp/tazo-sdk-rc1-pypi-verification.json
```

The version must again be `0.1.2rc1`, with `Summary: 8/8 passed`.

## Success criteria and the next stable release

- The build and publish jobs succeed for each target you test.
- Clean environments install `0.1.2rc1` from their intended index and pass `pip check`.
- The installed candidate passes all eight live verification cases and the
  retained-key smoke test.

Keep the workflow logs and local reports as your verification record. Publishing
a new candidate cannot change existing package files. Once satisfied, prepare a
separate stable `0.1.2` version bump, review/merge/tag it, and repeat the release
process in [RELEASING.md](RELEASING.md). Do not move the candidate tag or try to
rename an already-published candidate into a stable release.
