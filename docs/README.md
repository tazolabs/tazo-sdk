# SDK documentation

Customer documentation is published at
<https://docs.tazolabs.com/sdk/python/overview>. This repository contains the
SDK documentation sources and public API contracts, independently of the
serving application or the full Mintlify website.

- `source/`: Sphinx API reference, generated from the installed SDK.
- `mintlify/`: SDK customer guides and generated reference MDX.
- `../api/`: Public OpenAPI and AsyncAPI snapshots used by contract/sample tests.

## Build and check

From the repository root, install the Python docs group and Node dependencies.
Use Node.js 22 or 24 LTS for the existing pinned Mintlify converter:

```sh
uv sync --frozen --all-groups
npm ci
uv run --frozen --group docs python scripts/sdk_reference.py --check
uv run --frozen --group docs sphinx-build -W --keep-going -b html docs/source docs/_build/html
```

To update the reference after editing SDK docstrings or Sphinx sources:

```sh
uv run --frozen --group docs python scripts/sdk_reference.py
```

Commit the resulting `docs/mintlify/reference/*.mdx`. Sphinx JSON is generated
in a temporary directory; caches and HTML output are not committed. The Node
package is private development tooling and is not published to npm.

## Update Mintlify

The MDX files retain their deployed `/sdk/python/...` routes in links. They are
sources for the SDK portion of the existing website, not a separate Mintlify site.
When changing SDK docs, sync the reviewed `docs/mintlify/` contents to the site's
`api-docs/sdk/python/` directory and validate/preview them in that documentation
repository. Update the public API snapshots if the deployed contract changes.

As SDK ownership moves here, change the site's generation check to consume this
repository's reviewed output rather than regenerating from its historical SDK
source copy. Publication to GitHub or PyPI alone does not deploy Mintlify.
