# Set up the public SDK repository

This is a source-repository migration of the SDK already published as
`tazo-sdk==0.1.1`. The package name, `tazo` import, runtime code, and API origin
stay the same. PyPI and TestPyPI retain their existing projects and releases.
No upload is needed to make the source public.

## 1. Merge the import PR

Review the import PR and its installed-wheel and documentation CI checks, then
merge it into `main`. A small empty initial commit establishes the PR's base;
all imported SDK files are in the PR. Do not create a new `sdk-v0.1.1` release
tag here or move the original release tag. The runtime file hashes in
`compatibility/published-0.1.1.json` match both published 0.1.1 wheels.

After merging, update this checkout:

```sh
git switch main
git pull --ff-only origin main
```

## 2. Prepare a development environment

Use Python 3.11+ and uv. From the root of this repository:

```sh
uv sync --frozen --all-groups
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src/tazo examples
uv build --no-sources
uv run --frozen --group release python -m twine check dist/*
```

Documentation generation additionally uses Node.js 22 or 24 LTS and `npm ci`;
see [docs/README.md](docs/README.md). Protect `main` with PR review and the
installed-wheel and documentation checks if that matches your team's workflow.

## 3. Verify the existing PyPI release

Use a clean Python environment to test the published wheel, separately from the
editable development checkout:

```sh
python3 -m venv /tmp/tazo-sdk-public-pypi
source /tmp/tazo-sdk-public-pypi/bin/activate
env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL PIP_CONFIG_FILE=/dev/null \
  python -m pip install --index-url https://pypi.org/simple/ 'tazo-sdk==0.1.1'
python -m pip check
python -c 'import tazo; print(tazo.__version__, tazo.__file__)'
```

On Windows, create a virtual environment in a local folder and activate its
`Scripts/Activate.ps1`. The `env -u` commands above are for Bash/Zsh and isolate
pip from an unrelated private index; do not change your global pip configuration.

Set the raw `TAZO_API_KEY` in your terminal or secret store using a key created
at <https://app.tazolabs.com>. Do not paste it into a chat or commit it. Then,
from the repository root and this clean environment:

```sh
python examples/verify.py --audio examples/weather.wav \
  --expected-choice get_weather --output /tmp/tazo-sdk-pypi-verification.json
```

Expect `Summary: 8/8 passed`. This submits real decisions to
`https://api.tazolabs.com`; the recording says "What is the weather today?".
The tests run sequentially for both models in all four client modes.

To verify the existing TestPyPI release too, deactivate the first environment
and make another. Install dependencies from PyPI, then only the SDK from TestPyPI:

```sh
deactivate
python3 -m venv /tmp/tazo-sdk-public-testpypi
source /tmp/tazo-sdk-public-testpypi/bin/activate
env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL PIP_CONFIG_FILE=/dev/null \
  python -m pip install --index-url https://pypi.org/simple/ \
  'httpx>=0.27,<1' 'pydantic>=2.10,<3' 'websockets>=15,<18'
env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL PIP_CONFIG_FILE=/dev/null \
  python -m pip install --index-url https://test.pypi.org/simple/ \
  --no-deps 'tazo-sdk==0.1.1'
python -m pip check
python examples/verify.py --audio examples/weather.wav \
  --expected-choice get_weather --output /tmp/tazo-sdk-testpypi-verification.json
```

TestPyPI is a package index, not an API deployment. Both packages use the same
live Tazo API unless you explicitly pass another `--base-url` and a key valid there.

## 4. Configure GitHub environments for future releases

In **tazolabs/tazo-sdk → Settings → Environments**, create `testpypi` and `pypi`.
For each, under **Deployment branches and tags**, select **Selected branches
and tags** and add **Ref type: Tag**, pattern **`sdk-v*`**. A Branch rule with
that pattern does not allow a tag deployment.

Add an appropriate required reviewer to the `pypi` environment. No PyPI API token
needs to be stored as a GitHub secret; publishing uses OIDC.

## 5. Add trusted publishers to the existing package projects

Sign in with the accounts that already manage `tazo-sdk` on
[PyPI](https://pypi.org/manage/project/tazo-sdk/settings/) and
[TestPyPI](https://test.pypi.org/manage/project/tazo-sdk/settings/).
In each project's **Publishing** settings, add a GitHub trusted publisher:

| Field | TestPyPI | PyPI |
| --- | --- | --- |
| Owner | `tazolabs` | `tazolabs` |
| Repository | `tazo-sdk` | `tazo-sdk` |
| Workflow filename | `sdk-release.yml` | `sdk-release.yml` |
| Environment | `testpypi` | `pypi` |

Enter only the workflow filename. Add these publishers to the **existing**
projects; do not register a new pending project or change the package name.
Changing the source repository does not move users to a new PyPI project.
See the [official trusted-publisher instructions](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

## 6. Complete the publishing cutover

Future SDK changes belong in this public repository. Use [RELEASING.md](RELEASING.md)
when a new package release is needed. The first normal new version can be
`0.1.2`; leave `0.1.1` installed until then. Updated README/project URLs appear
on PyPI when that new version is uploaded, not when this GitHub PR is merged.

After confirming a future release publishes successfully from this repository,
remove the old serving-repository trusted publishers on both package indexes
and disable its SDK release workflow to keep one publishing source. The old
release history and artifacts remain valid.

The customer Mintlify site remains at <https://docs.tazolabs.com>. Coordinate its
SDK guides/reference with the sources here; see [docs/README.md](docs/README.md).
The serving repository's backend integration tests can keep testing the
published SDK while its old SDK source and docs-generation copy are retired
in a separate change. Do not delete backend tests as part of a package move.
