from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from datagraph.api.artifact import router as artifact_router
from datagraph.api.embeddings import router as embeddings_router
from datagraph.api.evidence import router as evidence_router
from datagraph.api.graphs import router as graphs_router
from datagraph.api.records import router as records_router
from datagraph.api.runs import router as runs_router
from datagraph.api.views import router as views_router
from datagraph.core.labeling import LabelProvider
from datagraph.core.openai_client import ClockFunc, EmbeddingProvider, SleepFunc
from datagraph.db import initialize_database
from datagraph.models import HealthResponse
from datagraph.runs.executor import RunExecutor
from datagraph.settings import Settings, load_settings

READ_ONLY_MESSAGE = "server is in read-only mode; mutating endpoints are disabled"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class ImmutableAssetStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict) -> FileResponse:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def create_app(
    settings: Settings | None = None,
    *,
    embedding_provider_factory: Callable[[dict], EmbeddingProvider] | None = None,
    label_provider_factory: Callable[[dict], LabelProvider] | None = None,
    clock: ClockFunc | None = None,
    sleep: SleepFunc | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        initialize_database(resolved_settings.db_path)
        executor = RunExecutor(
            resolved_settings.db_path,
            embedding_provider_factory=embedding_provider_factory,
            label_provider_factory=label_provider_factory,
            openai_api_key=resolved_settings.openai_api_key,
            clock=clock,
            sleep=sleep,
        )
        executor.recover_interrupted()
        await executor.start()
        app.state.settings = resolved_settings
        app.state.run_executor = executor
        try:
            yield
        finally:
            await executor.stop()

    app = FastAPI(title="Data Graph", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def read_only_guard(request: Request, call_next: Callable) -> JSONResponse:
        settings_for_request = getattr(request.app.state, "settings", resolved_settings)
        if (
            settings_for_request.read_only
            and request.method in MUTATING_METHODS
            and not _read_only_mutation_allowed(request)
        ):
            return JSONResponse(
                {"detail": READ_ONLY_MESSAGE},
                status_code=status.HTTP_403_FORBIDDEN,
            )
        return await call_next(request)

    app.include_router(graphs_router)
    app.include_router(records_router)
    app.include_router(embeddings_router)
    app.include_router(runs_router)
    app.include_router(views_router)
    app.include_router(evidence_router)
    app.include_router(artifact_router)

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    dist_dir = resolved_settings.dist_dir
    if dist_dir.exists():
        assets_dir = dist_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", ImmutableAssetStaticFiles(directory=assets_dir), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(dist_dir / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def _read_only_mutation_allowed(request: Request) -> bool:
    path = request.url.path.rstrip("/")
    return (
        request.method == "POST"
        and path.startswith("/api/graphs/")
        and path.endswith("/evidence")
    )


app = create_app()


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "datagraph.main:app",
        host="127.0.0.1",
        port=settings.port,
        reload=False,
        # On this Python 3.14 venv, uvicorn's standard transports reset HTTP
        # requests; h11 + asyncio keeps local serving stable.
        loop="asyncio",
        http="h11",
    )


if __name__ == "__main__":
    main()
