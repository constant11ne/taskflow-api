"""TaskFlow: небольшой учебный API для управления личными задачами."""

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import perf_counter

from dotenv import load_dotenv
from fastapi import FastAPI, Request

logger = logging.getLogger("taskflow")


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

    return application


load_dotenv()
app = create_app()
