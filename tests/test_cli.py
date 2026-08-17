from fastapi.testclient import TestClient

from aisbench_web.app import create_app
from aisbench_web.cli import parse_args


def test_parse_args_uses_simple_server_defaults():
    args = parse_args([])
    assert args.host == "0.0.0.0"
    assert args.port == 8000
    assert args.max_concurrent_jobs == 1


def test_health_endpoint_reports_service_name(tmp_path):
    app = create_app(data_dir=tmp_path, start_worker=False)
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "aisbench-web"}
