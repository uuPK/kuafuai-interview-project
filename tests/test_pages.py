from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_page_and_static_security_headers_without_credentials(tmp_path):
    with TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path))) as client:
        for path in ("/", "/assets/app.js", "/assets/app.css"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert "script-src 'self'" in response.headers["content-security-policy"]
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"
        assert client.get("/assets/../../.env").status_code == 404
        assert client.get("/assets/news.sqlite3").status_code == 404
        assert 'type="password"' in client.get("/").text
