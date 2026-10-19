"""TaskFlow: небольшой учебный API для управления личными задачами."""

import logging
import os
from copy import deepcopy
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from time import perf_counter
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger("taskflow")


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    done = "done"


class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    priority: Priority = Priority.normal

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Название задачи не может состоять из пробелов")
        return value


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: TaskStatus


class Task(TaskCreate):
    id: int
    status: TaskStatus = TaskStatus.todo
    created_at: datetime


class TaskStore:
    """Хранилище в памяти; блокировка защищает операции параллельных запросов."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.tasks: dict[int, Task] = {}
        self.next_id = 1
        self.lock = Lock()

    def create(self, payload: TaskCreate) -> Task:
        with self.lock:
            if len(self.tasks) >= self.capacity:
                raise HTTPException(409, "Достигнут лимит задач MAX_TASKS")
            task = Task(
                id=self.next_id,
                created_at=datetime.now(timezone.utc),
                **payload.model_dump(),
            )
            self.tasks[task.id] = task
            self.next_id += 1
            return task.model_copy(deep=True)

    def snapshot(self) -> list[Task]:
        with self.lock:
            return deepcopy(list(self.tasks.values()))

    def get(self, task_id: int) -> Task:
        with self.lock:
            return self._require(task_id).model_copy(deep=True)

    def update(self, task_id: int, task_status: TaskStatus) -> Task:
        with self.lock:
            task = self._require(task_id)
            task.status = task_status
            return task.model_copy(deep=True)

    def delete(self, task_id: int):
        with self.lock:
            self._require(task_id)
            del self.tasks[task_id]

    def _require(self, task_id: int) -> Task:
        if task_id not in self.tasks:
            raise HTTPException(404, "Задача не найдена")
        return self.tasks[task_id]


TaskId = Annotated[int, Path(ge=1, description="Положительный идентификатор задачи")]


@dataclass(frozen=True)
class Settings:
    app_name: str = "TaskFlow API"
    app_version: str = "1.0.0"
    max_tasks: int = 1000
    log_level: str = "INFO"

    def __post_init__(self):
        if not self.app_name.strip() or not self.app_version.strip():
            raise ValueError("APP_NAME and APP_VERSION must not be empty")
        if self.max_tasks < 1:
            raise ValueError("MAX_TASKS must be a positive integer")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")

    @classmethod
    def from_env(cls):
        return cls(
            app_name=os.getenv("APP_NAME", "TaskFlow API"),
            app_version=os.getenv("APP_VERSION", "1.0.0"),
            max_tasks=int(os.getenv("MAX_TASKS", "1000")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Фабрика позволяет каждому тесту использовать отдельное приложение."""
    config = settings if settings is not None else Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        logging.basicConfig(
            level=config.log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        logger.setLevel(config.log_level)
        logger.info("Service started name=%s version=%s", config.app_name, config.app_version)
        yield
        logger.info("Service stopped")

    application = FastAPI(
        title=config.app_name,
        version=config.app_version,
        description="Создание задач, поиск, изменение статуса и статистика выполнения.",
        lifespan=lifespan,
    )
    application.state.settings = config
    store = TaskStore(config.max_tasks)

    @application.middleware("http")
    async def log_requests(request: Request, call_next):
        started = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("Request failed method=%s path=%s", request.method, request.url.path)
            raise
        logger.info(
            "Request method=%s path=%s status=%s duration_ms=%.2f",
            request.method, request.url.path, response.status_code,
            (perf_counter() - started) * 1000,
        )
        return response

    @application.get("/", tags=["Сервис"])
    def root():
        return {"name": config.app_name, "docs": "/docs", "health": "/health"}

    @application.get("/health", tags=["Сервис"])
    def health():
        return {"status": "ok"}

    @application.get("/version", tags=["Сервис"])
    def version():
        return {"name": config.app_name, "version": config.app_version}

    @application.get("/tasks", response_model=list[Task], tags=["Задачи"])
    def list_tasks():
        return store.snapshot()

    @application.post(
        "/tasks", response_model=Task,
        status_code=status.HTTP_201_CREATED, tags=["Задачи"],
    )
    def create_task(payload: TaskCreate, response: Response):
        task = store.create(payload)
        response.headers["Location"] = f"/tasks/{task.id}"
        logger.info("Task created id=%s priority=%s", task.id, task.priority.value)
        return task

    @application.get("/tasks/{task_id}", response_model=Task, tags=["Задачи"])
    def get_task(task_id: TaskId):
        return store.get(task_id)

    @application.patch("/tasks/{task_id}", response_model=Task, tags=["Задачи"])
    def update_task(task_id: TaskId, payload: TaskUpdate):
        task = store.update(task_id, payload.status)
        logger.info("Task updated id=%s status=%s", task.id, task.status.value)
        return task

    @application.delete(
        "/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Задачи"],
    )
    def delete_task(task_id: TaskId):
        store.delete(task_id)
        logger.info("Task deleted id=%s", task_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return application


load_dotenv()
app = create_app()
