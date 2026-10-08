"""Application factory."""

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from app.core.config import get_settings
from app.core.errors import ErrorEnvelope, register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import CorrelationMiddleware
from app.modules.crm.companies_router import router as companies_router
from app.modules.crm.records_router import router as records_router
from app.modules.crm.sales_router import router as sales_router
from app.modules.discovery.router import router as discovery_router
from app.modules.identity.auth_router import router as auth_router
from app.modules.identity.tenant_router import router as tenant_router
from app.modules.prospects.router import router as prospects_router
from app.modules.providers.router import router as providers_router
from app.modules.research.router import router as research_router
from app.modules.system.router import api_router as system_api_router
from app.modules.system.router import router as health_router

API_PREFIX = "/api/v1"


def _operation_id(route: APIRoute) -> str:
    return route.operation_id or route.name


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="SEWEB CRM API",
        version="1.0.0",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None if settings.is_production_like else f"{API_PREFIX}/docs",
        redoc_url=None,
        generate_unique_id_function=_operation_id,
        responses={
            401: {"model": ErrorEnvelope},
            403: {"model": ErrorEnvelope},
            404: {"model": ErrorEnvelope},
            422: {"model": ErrorEnvelope},
        },
    )
    app.add_middleware(CorrelationMiddleware)
    register_error_handlers(app)

    app.include_router(health_router)
    v1 = APIRouter(prefix=API_PREFIX)
    v1.include_router(system_api_router)
    v1.include_router(auth_router)
    v1.include_router(tenant_router)
    v1.include_router(companies_router)
    v1.include_router(sales_router)
    v1.include_router(records_router)
    v1.include_router(research_router)
    v1.include_router(prospects_router)
    v1.include_router(providers_router)
    v1.include_router(discovery_router)
    app.include_router(v1)
    return app


app = create_app()
