"""An integer the database cannot hold is the client's mistake: 422, never a
500 from the driver ("Python int too large to convert to SQLite INTEGER",
"integer out of range" on PostgreSQL).

The cases are derived from app.openapi(), so an endpoint added later with an
integer path or query parameter, or an integer field in its JSON body, is
covered without touching this file.
"""

import pytest
from payment_support import Shop

from app.main import app

# Past the column in either direction: the value and the rule it breaks.
BEYOND = [(2**70, "less_than_equal"), (-(2**70), "greater_than_equal")]
INT32_MAX = 2**31 - 1

_SCHEMA = app.openapi()
_METHODS = {"get", "post", "put", "patch", "delete"}


def _is_integer(schema):
    if schema.get("type") == "integer":
        return True
    return any(option.get("type") == "integer" for option in schema.get("anyOf", []))


def _body_integers(operation):
    body = operation.get("requestBody", {}).get("content", {}).get("application/json")
    if body is None or "$ref" not in body["schema"]:
        return []
    model = _SCHEMA["components"]["schemas"][body["schema"]["$ref"].rsplit("/", 1)[-1]]
    return [name for name, prop in model.get("properties", {}).items() if _is_integer(prop)]


def _cases():
    path_query, bodies = [], []
    for path, item in _SCHEMA["paths"].items():
        for method, operation in item.items():
            if method not in _METHODS:
                continue
            params = operation.get("parameters", [])
            for param in params:
                if param["in"] in ("path", "query") and _is_integer(param["schema"]):
                    path_query.append(
                        pytest.param(method, path, params, param["in"], param["name"],
                                     id=f"{method.upper()} {path} {param['in']}:{param['name']}")
                    )
            for name in _body_integers(operation):
                bodies.append(
                    pytest.param(method, path, params, name,
                                 id=f"{method.upper()} {path} body:{name}")
                )
    return path_query, bodies


_PATH_QUERY_CASES, _BODY_CASES = _cases()


def _url(path, params, overrides):
    """The path with every parameter filled in: the overridden ones with
    their value, an integer with 1, anything else with a placeholder."""
    query = {}
    for param in params:
        value = overrides.get(param["name"], 1 if _is_integer(param["schema"]) else "x")
        if param["in"] == "path":
            path = path.replace("{" + param["name"] + "}", str(value))
        elif param["in"] == "query" and (param["name"] in overrides or param.get("required")):
            query[param["name"]] = value
    return path, query


def _out_of_range_errors(response, where, name, rule):
    assert response.status_code == 422, response.text
    return [
        error for error in response.json()["detail"]
        if error["loc"][:2] == [where, name] and error["type"] == rule
    ]


@pytest.fixture
def admin_headers(client, db_session):
    return Shop(client, db_session).admin()


def test_the_derivation_found_the_endpoints():
    assert len(_PATH_QUERY_CASES) > 30
    assert len(_BODY_CASES) > 8


@pytest.mark.parametrize("value, rule", BEYOND)
@pytest.mark.parametrize("method, path, params, where, name", _PATH_QUERY_CASES)
def test_integer_parameter_beyond_the_database_is_422(
    client, admin_headers, method, path, params, where, name, value, rule
):
    url, query = _url(path, params, {name: value})

    response = client.request(method, url, params=query, headers=admin_headers, json={})

    assert _out_of_range_errors(response, where, name, rule)


@pytest.mark.parametrize("value, rule", BEYOND)
@pytest.mark.parametrize("method, path, params, name", _BODY_CASES)
def test_integer_body_field_beyond_the_database_is_422(
    client, admin_headers, method, path, params, name, value, rule
):
    url, query = _url(path, params, {})

    response = client.request(method, url, params=query, headers=admin_headers,
                              json={name: value})

    assert _out_of_range_errors(response, "body", name, rule)


def test_the_largest_integer_the_database_holds_still_answers_normally(client, admin_headers):
    assert client.get(f"/api/v1/products/{INT32_MAX}").status_code == 404
    assert client.get("/api/v1/products", params={"page": INT32_MAX}).status_code == 200
    assert client.get("/api/v1/categories", params={"page": INT32_MAX}).status_code == 200
