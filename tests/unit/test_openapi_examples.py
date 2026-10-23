from app.main import app


def test_schemas_use_examples_not_the_deprecated_example_keyword():
    """OpenAPI 3.1 deprecates the single-valued `example`; FastAPI's docs say to
    declare `examples: [...]` in json_schema_extra."""
    schemas = app.openapi()["components"]["schemas"]
    assert [name for name, schema in schemas.items() if "example" in schema] == []
    assert schemas["ConsentUpdate"]["examples"] == [
        {"marketing_consent": True, "privacy_policy_accepted": True}
    ]
