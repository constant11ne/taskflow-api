"""Проверка публичного контракта API без сети и внешних сервисов."""

import logging

import pytest
from fastapi.testclient import TestClient

from app_final import Settings, create_app


@pytest.fixture
def client():
    with TestClient(create_app(Settings())) as test_client:
        yield test_client


def add_task(client, title="Подготовить проект", priority="normal"):
    response = client.post("/tasks", json={"title": title, "priority": priority})
    assert response.status_code == 201
    return response.json()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_and_name_from_environment(monkeypatch):
    monkeypatch.setenv("APP_NAME", "Study Tasks")
    monkeypatch.setenv("APP_VERSION", "2.3.4")
    monkeypatch.setenv("MAX_TASKS", "7")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/version").json() == {"name": "Study Tasks", "version": "2.3.4"}
    assert app.state.settings.max_tasks == 7
    assert app.state.settings.log_level == "DEBUG"


def test_empty_list(client):
    assert client.get("/tasks").json() == []


def test_create_and_read_task(client):
    response = client.post("/tasks", json={"title": "  Написать тесты  ", "priority": "high"})
    assert response.status_code == 201
    task = response.json()
    assert task["title"] == "Написать тесты"
    assert task["status"] == "todo"
    assert task["priority"] == "high"
    assert task["created_at"]
    assert response.headers["location"] == f"/tasks/{task['id']}"
    assert client.get(response.headers["location"]).json() == task


def test_update_status(client):
    task = add_task(client)
    response = client.patch(f"/tasks/{task['id']}", json={"status": "done"})
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert client.get(f"/tasks/{task['id']}").json()["status"] == "done"


def test_delete_task_and_do_not_reuse_identifiers(client):
    task = add_task(client)
    response = client.delete(f"/tasks/{task['id']}")
    assert response.status_code == 204
    assert response.content == b""
    assert client.get(f"/tasks/{task['id']}").status_code == 404
    assert add_task(client)["id"] > task["id"]


@pytest.mark.parametrize("method,kwargs", [
    ("get", {}), ("patch", {"json": {"status": "done"}}), ("delete", {}),
])
def test_missing_task_returns_404(client, method, kwargs):
    response = getattr(client, method)("/tasks/999", **kwargs)
    assert response.status_code == 404
    assert response.json()["detail"] == "Задача не найдена"


@pytest.mark.parametrize("payload", [
    {"title": ""}, {"title": "   "}, {"title": "x" * 121},
    {"title": "Task", "priority": "urgent"},
    {"title": "Task", "description": "x" * 1001},
    {"title": "Task", "unknown": True}, {},
])
def test_invalid_task_returns_422_without_creating_data(client, payload):
    assert client.post("/tasks", json=payload).status_code == 422
    assert client.get("/tasks").json() == []


@pytest.mark.parametrize("path", [
    "/tasks/0", "/tasks/-1", "/tasks/not-a-number",
    "/tasks?status=unknown", "/tasks?priority=urgent",
    "/tasks?limit=0", "/tasks?limit=101", "/tasks?offset=-1", "/tasks?q=",
])
def test_invalid_parameters(client, path):
    assert client.get(path).status_code == 422


def test_invalid_status_does_not_change_task(client):
    task = add_task(client)
    assert client.patch(f"/tasks/{task['id']}", json={"status": "unknown"}).status_code == 422
    assert client.get(f"/tasks/{task['id']}").json()["status"] == "todo"


def test_combined_filters_and_case_insensitive_search(client):
    first = add_task(client, "Тесты API", "high")
    add_task(client, "Тесты документации", "normal")
    add_task(client, "Запустить API", "high")
    client.patch(f"/tasks/{first['id']}", json={"status": "done"})
    response = client.get("/tasks", params={"status": "done", "priority": "high", "q": "тЕсТы"})
    assert response.status_code == 200
    assert [task["id"] for task in response.json()] == [first["id"]]
    assert client.get("/tasks", params={"q": "несуществующая"}).json() == []


def test_pagination(client):
    tasks = [add_task(client, f"Task {index}") for index in range(3)]
    response = client.get("/tasks?offset=1&limit=1")
    assert [task["id"] for task in response.json()] == [tasks[1]["id"]]
    assert client.get("/tasks?offset=10").json() == []


def test_statistics_empty_and_after_changes(client):
    assert client.get("/stats").json() == {
        "total": 0, "todo": 0, "in_progress": 0, "done": 0, "completion_percent": 0.0,
    }
    tasks = [add_task(client) for _ in range(3)]
    client.patch(f"/tasks/{tasks[0]['id']}", json={"status": "done"})
    client.patch(f"/tasks/{tasks[1]['id']}", json={"status": "in_progress"})
    assert client.get("/stats").json() == {
        "total": 3, "todo": 1, "in_progress": 1, "done": 1, "completion_percent": 33.3,
    }
    client.delete(f"/tasks/{tasks[0]['id']}")
    assert client.get("/stats").json()["total"] == 2


def test_capacity_limit_and_recovery_after_delete():
    with TestClient(create_app(Settings(max_tasks=1))) as client:
        task = add_task(client)
        assert client.post("/tasks", json={"title": "Too many"}).status_code == 409
        client.delete(f"/tasks/{task['id']}")
        assert add_task(client)["id"] == 2


@pytest.mark.parametrize("changes", [
    {"max_tasks": 0}, {"max_tasks": -1}, {"log_level": "INVALID"},
    {"app_name": " "}, {"app_version": ""},
])
def test_invalid_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        Settings(**changes)


def test_logs_include_lifecycle_requests_and_task_events(caplog):
    with caplog.at_level(logging.INFO, logger="taskflow"):
        with TestClient(create_app(Settings())) as client:
            add_task(client)
    assert "Service started" in caplog.text
    assert "Task created id=1" in caplog.text
    assert "method=POST path=/tasks status=201" in caplog.text
    assert "Service stopped" in caplog.text


def test_documentation_and_required_routes(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert {"/health", "/version", "/tasks", "/tasks/{task_id}", "/stats"} <= schema["paths"].keys()


def test_applications_have_independent_storage(client):
    add_task(client)
    with TestClient(create_app(Settings())) as other:
        assert other.get("/tasks").json() == []
