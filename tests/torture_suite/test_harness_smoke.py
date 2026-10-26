"""Smoke test: the torture suite sees the project's fixtures and helpers."""
import torture_support  # noqa: F401  (puts tests/integration on sys.path)


def test_project_fixtures_reachable(client, db_session):
    import payment_support  # noqa: F401

    response = client.get("/api/v1/products")
    assert response.status_code == 200
