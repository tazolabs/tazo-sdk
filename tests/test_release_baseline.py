"""The imported 0.1.1 runtime must stay identical to both published wheels."""

import hashlib
import importlib.util
import json
from importlib.resources import files
from pathlib import Path

import pytest

from tazo import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_imported_runtime_matches_published_release():
    baseline = json.loads((ROOT / "compatibility/published-0.1.1.json").read_text())
    if __version__ != baseline["version"]:
        pytest.skip("This historical baseline applies only to the imported 0.1.1 release.")
    expected = baseline["runtime_files"]
    source = {
        f"tazo/{path.name}": hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (ROOT / "src/tazo").iterdir()
        if path.is_file()
    }
    assert source == expected
    for name, digest in expected.items():
        installed = files("tazo").joinpath(name.removeprefix("tazo/"))
        assert hashlib.sha256(installed.read_bytes()).hexdigest() == digest


def test_release_guard_rejects_republication_and_requires_matching_tag(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "sdk_release_guard", ROOT / "scripts/check_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    (tmp_path / "src/tazo").mkdir(parents=True)

    def version(manifest, runtime):
        (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{manifest}"\n')
        (tmp_path / "src/tazo/_version.py").write_text(f'__version__ = "{runtime}"\n')

    version("0.1.1", "0.1.1")
    with pytest.raises(ValueError, match="already published"):
        module.check_release("tag", "sdk-v0.1.1")
    version("0.1.2", "0.1.1")
    with pytest.raises(ValueError, match="same version"):
        module.check_release("tag", "sdk-v0.1.2")
    version("0.1.2", "0.1.2")
    with pytest.raises(ValueError, match="reviewed release tag"):
        module.check_release("branch", "main")
    with pytest.raises(ValueError, match="reviewed release tag"):
        module.check_release("tag", "sdk-v0.2.0")
    assert module.check_release("tag", "sdk-v0.1.2") == "0.1.2"
