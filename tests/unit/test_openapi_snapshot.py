"""The OpenAPI document is the API's contract; a change to it must be a choice.

app.openapi() is compared with tests/snapshots/openapi.json. When they differ
the failure lists what changed (paths, operations, schemas), so a pull request
that alters the contract by accident, or without saying so, fails here instead
of reaching a client. After an intended change, regenerate the file and commit
it with the change:

    UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/unit/test_openapi_snapshot.py

(the test then rewrites the snapshot and passes; review the git diff).

info.version is left out of the comparison: it is the package version, which
changes at every release (tests/unit/test_version.py checks it against
pyproject.toml), so keeping it would force a snapshot edit that says nothing
about the contract. Everything else is compared as generated. The file is
written with sorted keys and a fixed indent, so the same schema always gives
the same bytes.
"""
import json
import os
from pathlib import Path

from app.main import app

SNAPSHOT = Path(__file__).resolve().parent.parent / "snapshots" / "openapi.json"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _current() -> dict:
    document = json.loads(json.dumps(app.openapi()))  # a copy, JSON types only
    document["info"].pop("version", None)
    return document


def _write(document: dict) -> None:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    with SNAPSHOT.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")


def _operations(document: dict) -> dict:
    return {
        (method.upper(), path): operation
        for path, item in document.get("paths", {}).items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    }


def _path_extras(item: dict) -> dict:
    return {key: value for key, value in item.items() if key not in HTTP_METHODS}


def summarize_differences(expected: dict, actual: dict) -> list[str]:
    """What differs between two OpenAPI documents, one line per change."""
    lines = []

    expected_ops, actual_ops = _operations(expected), _operations(actual)
    for key in sorted(actual_ops.keys() - expected_ops.keys()):
        lines.append(f"operation added:   {key[0]} {key[1]}")
    for key in sorted(expected_ops.keys() - actual_ops.keys()):
        lines.append(f"operation removed: {key[0]} {key[1]}")
    for key in sorted(expected_ops.keys() & actual_ops.keys()):
        if expected_ops[key] != actual_ops[key]:
            changed = sorted(
                part for part in expected_ops[key].keys() | actual_ops[key].keys()
                if expected_ops[key].get(part) != actual_ops[key].get(part)
            )
            lines.append(f"operation changed: {key[0]} {key[1]} ({', '.join(changed)})")

    expected_schemas = expected.get("components", {}).get("schemas", {})
    actual_schemas = actual.get("components", {}).get("schemas", {})
    for name in sorted(actual_schemas.keys() - expected_schemas.keys()):
        lines.append(f"schema added:      {name}")
    for name in sorted(expected_schemas.keys() - actual_schemas.keys()):
        lines.append(f"schema removed:    {name}")
    for name in sorted(expected_schemas.keys() & actual_schemas.keys()):
        if expected_schemas[name] != actual_schemas[name]:
            lines.append(f"schema changed:    {name}")

    # Anything else (security schemes, info, parameters under components...).
    def rest(document):
        remaining = {k: v for k, v in document.items() if k != "paths"}
        remaining["components"] = {
            k: v for k, v in document.get("components", {}).items() if k != "schemas"
        }
        return remaining

    expected_rest, actual_rest = rest(expected), rest(actual)
    for part in sorted(expected_rest.keys() | actual_rest.keys()):
        if expected_rest.get(part) != actual_rest.get(part):
            lines.append(f"document part changed: {part}")

    # A path-level difference that is not an operation (summary, parameters).
    for path in sorted(expected.get("paths", {}).keys() & actual.get("paths", {}).keys()):
        if _path_extras(expected["paths"][path]) != _path_extras(actual["paths"][path]):
            lines.append(f"path item changed: {path}")
    return lines


def test_the_openapi_document_matches_the_snapshot():
    actual = _current()
    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT") == "1":
        _write(actual)
        return
    assert SNAPSHOT.exists(), (
        f"{SNAPSHOT} is missing; create it with "
        "UPDATE_OPENAPI_SNAPSHOT=1 pytest tests/unit/test_openapi_snapshot.py"
    )
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if expected != actual:
        changes = summarize_differences(expected, actual) or ["(no structured difference found)"]
        raise AssertionError(
            "The OpenAPI document changed. If that is intended, regenerate the snapshot with\n"
            "  UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/unit/test_openapi_snapshot.py\n"
            "and commit tests/snapshots/openapi.json. Changes:\n  " + "\n  ".join(changes)
        )


def test_the_snapshot_file_is_deterministic_json():
    # Written the way _write writes it: re-serializing what is on disk
    # changes nothing, so a regenerated snapshot only differs by content.
    text = SNAPSHOT.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_the_summary_names_added_removed_and_changed_parts():
    base = {
        "info": {"title": "t"},
        "paths": {"/a": {"get": {"responses": {"200": {}}}}, "/b": {"post": {}}},
        "components": {"schemas": {"X": {"type": "string"}, "Y": {"type": "integer"}}},
    }
    changed = json.loads(json.dumps(base))
    del changed["paths"]["/b"]
    changed["paths"]["/c"] = {"put": {}}
    changed["paths"]["/a"]["get"]["responses"]["404"] = {}
    changed["components"]["schemas"]["X"]["type"] = "number"
    del changed["components"]["schemas"]["Y"]
    changed["components"]["schemas"]["Z"] = {}
    assert summarize_differences(base, changed) == [
        "operation added:   PUT /c",
        "operation removed: POST /b",
        "operation changed: GET /a (responses)",
        "schema added:      Z",
        "schema removed:    Y",
        "schema changed:    X",
    ]
