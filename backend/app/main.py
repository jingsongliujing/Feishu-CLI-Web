from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import auth, chat, health, lark_setup, models
from app.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    print(f"{settings.APP_NAME} starting...")
    yield
    print(f"{settings.APP_NAME} shutting down...")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.APP_NAME,
        description="A standalone web UI and API for Lark/Feishu CLI automation.",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, tags=["Health"])
    app.include_router(auth.router, prefix=settings.API_PREFIX, tags=["Auth"])
    app.include_router(chat.router, prefix=settings.API_PREFIX, tags=["Chat"])
    app.include_router(lark_setup.router, prefix=settings.API_PREFIX, tags=["Lark CLI Setup"])
    app.include_router(models.router, prefix=settings.API_PREFIX, tags=["Model Config"])
    return app


app = create_app()
