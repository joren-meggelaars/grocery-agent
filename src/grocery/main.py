import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from grocery.config import Settings, get_settings
from grocery.db.session import make_engine, make_session_factory
from grocery.security.deps import csrf_protect, require_auth
from grocery.security.middleware import install_security_middleware
from grocery.web import routes_auth, routes_home
from grocery.web.templating import STATIC_DIR


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(
        title="Grocery Agent",
        # No interactive docs / schema: nothing to browse unauthenticated.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        # Deny by default: every route needs a session unless marked @public.
        dependencies=[Depends(require_auth), Depends(csrf_protect)],
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    install_security_middleware(app)
    app.include_router(routes_home.router)
    app.include_router(routes_auth.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
