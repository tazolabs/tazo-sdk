import ast
import importlib.metadata
from pathlib import Path

import yaml
from conftest import ready

from tazo import Tazo
from tazo.types import SessionReady

ROOT = Path(__file__).resolve().parents[1]


def test_customer_endpoint_allowlist():
    document = yaml.safe_load((ROOT / "api/openapi.yaml").read_text())
    operations = {
        (method.upper(), path)
        for path, item in document["paths"].items()
        for method in item
        if method in {"get", "post", "put", "delete", "patch"}
    }
    assert operations == {("POST", "/v1/decisions"), ("GET", "/v1/models"), ("GET", "/healthz")}
    asyncapi = yaml.safe_load((ROOT / "api/asyncapi.yaml").read_text())
    assert {c["address"] for c in asyncapi["channels"].values()} == {"/v1/decisions/stream"}
    for spec in (document, asyncapi):

        def refs(value, document=spec):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "$ref" and child.startswith("#/"):
                        target = document
                        for part in child[2:].split("/"):
                            target = target[part.replace("~1", "/").replace("~0", "~")]
                    else:
                        refs(child)
            elif isinstance(value, list):
                for child in value:
                    refs(child)

        refs(spec)
    with Tazo() as client:
        assert {x for x in dir(client.decisions) if not x.startswith("_")} == {"create", "stream"}
        assert {x for x in dir(client.models) if not x.startswith("_")} == {"list"}
        assert {x for x in dir(client.health) if not x.startswith("_")} == {"check"}
        assert not hasattr(client, "api_keys") and not hasattr(client, "support")


def test_idempotency_and_public_question_types_in_spec():
    from tazo.types import ChoiceQuestion, DecisionRequest, NoulQuestion

    spec = yaml.safe_load((ROOT / "api/openapi.yaml").read_text())
    operation = spec["paths"]["/v1/decisions"]["post"]
    assert operation["parameters"][0]["name"] == "Idempotency-Key"
    assert "409" in operation["responses"]
    for name in ("Question", "Answer"):
        refs = spec["components"]["schemas"][name]["oneOf"]
        assert len(refs) == 2 and all("Score" not in r["$ref"] for r in refs)
    for model in (DecisionRequest, ChoiceQuestion, NoulQuestion):
        sdk_schema = model.model_json_schema()
        public_schema = spec["components"]["schemas"][model.__name__]
        assert set(sdk_schema["properties"]) == set(public_schema["properties"])


def test_sdk_has_no_backend_dependency_and_version_matches():
    import tomllib
    from importlib.resources import files

    from tazo import __version__

    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert manifest["project"]["version"] == __version__ == importlib.metadata.version("tazo-sdk")
    assert files("tazo").joinpath("py.typed").is_file()
    assert {dep.split(">=")[0] for dep in manifest["project"]["dependencies"]} == {
        "httpx",
        "pydantic",
        "websockets",
    }
    for file in (ROOT / "src/tazo").glob("*.py"):
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imports = [node.module or ""]
            else:
                continue
            assert not any(
                name.split(".")[0]
                in {"tazo_api", "tazo_runtime", "tazo_contracts", "boto3", "numpy", "fastapi"}
                for name in imports
            )


def test_streaming_duration_limit_matches_nullable_public_contract():
    spec = yaml.safe_load((ROOT / "api/asyncapi.yaml").read_text())
    schemas = spec["components"]["schemas"]
    assert schemas["SessionStart"]["properties"]["audio_scope"]["default"] == "chunk"
    wire_limit = schemas["SessionReady"]["properties"]["max_turn_audio_ms"]
    sdk_limit = SessionReady.model_json_schema()["properties"]["max_turn_audio_ms"]
    for schema in (wire_limit, sdk_limit):
        assert {option["type"] for option in schema["anyOf"]} == {"integer", "null"}
    assert SessionReady.model_validate(ready(max_turn_audio_ms=None)).max_turn_audio_ms is None
    assert SessionReady.model_validate(ready(max_turn_audio_ms=30000)).max_turn_audio_ms == 30000
