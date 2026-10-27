"""Check a release's version and reviewed tag before building or publishing."""

from __future__ import annotations

import ast
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_release(ref_type: str, ref_name: str) -> str:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    tree = ast.parse((ROOT / "src/tazo/_version.py").read_text())
    if ast.literal_eval(tree.body[0].value) != version:
        raise ValueError("pyproject.toml and src/tazo/_version.py must have the same version.")
    if version in {"0.1.0", "0.1.1"}:
        raise ValueError(
            f"{version} is already published. Keep the existing release; prepare a new version "
            "(for example 0.1.2) before publishing from this repository."
        )
    if ref_type != "tag" or ref_name != f"sdk-v{version}":
        raise ValueError(f"Dispatch from the reviewed release tag sdk-v{version}.")
    return version


if __name__ == "__main__":
    try:
        print(
            check_release(
                os.environ.get("GITHUB_REF_TYPE", ""), os.environ.get("GITHUB_REF_NAME", "")
            )
        )
    except ValueError as error:
        raise SystemExit(str(error)) from None
