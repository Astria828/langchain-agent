"""阶段 0 的应用启动、统一响应与 CORS 测试。"""

from app.core.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient

TEST_FRONTEND_ORIGIN = "http://test-frontend.local"
client = TestClient(
    create_app(
        Settings(
            environment="test",
            frontend_origins=TEST_FRONTEND_ORIGIN,
        )
    )
)


def test_health_uses_api_contract() -> None:
    """健康检查返回统一响应壳和同一 request_id。"""

    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"] == {"status": "ok"}
    assert payload["requestId"].startswith("req_")
    assert response.headers["X-Request-ID"] == payload["requestId"]


def test_missing_route_uses_unified_error() -> None:
    """框架级 404 也必须遵守统一错误结构。"""

    response = client.get("/api/not-found")

    assert response.status_code == 404
    payload = response.json()
    assert payload["code"] == "RESOURCE_NOT_FOUND"
    assert payload["requestId"] == response.headers["X-Request-ID"]


def test_local_frontend_origin_is_allowed() -> None:
    """测试环境指定的前端来源可以完成 CORS 预检。"""

    response = client.options(
        "/api/health",
        headers={
            "Origin": TEST_FRONTEND_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == TEST_FRONTEND_ORIGIN


def test_health_openapi_schema_is_explicit() -> None:
    """健康检查在 OpenAPI 中引用明确的响应模型。"""

    schema = client.get("/openapi.json").json()
    response_schema = schema["paths"]["/api/health"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]

    assert response_schema == {"$ref": "#/components/schemas/HealthResponse"}
    properties = schema["components"]["schemas"]["HealthResponse"]["properties"]
    assert "requestId" in properties
