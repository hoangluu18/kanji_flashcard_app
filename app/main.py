from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.catalog import CatalogError, load_catalog, seed_catalog
from app.config import Settings, get_settings
from app.database import Base, build_engine, build_session_factory, session_scope
from app.logging_setup import configure_logging, get_logger
from app.models import Kanji, KanjiCard
from app.scheduler_service import SchedulerService
from app.telegram_service import TelegramBotService

logger = get_logger(__name__)


@dataclass
class RuntimeState:
    settings: Settings
    engine: object
    session_factory: object
    telegram: TelegramBotService
    scheduler: SchedulerService


settings = get_settings()
configure_logging(settings.app_log_level)

engine = build_engine(settings)
session_factory = build_session_factory(engine)
Base.metadata.create_all(bind=engine)

telegram_service = TelegramBotService(settings=settings, session_factory=session_factory)
scheduler_service = SchedulerService(
    settings=settings,
    session_factory=session_factory,
    bot_service=telegram_service,
)

app = FastAPI(title=settings.app_name, version="0.1.0")
app.state.runtime = RuntimeState(
    settings=settings,
    engine=engine,
    session_factory=session_factory,
    telegram=telegram_service,
    scheduler=scheduler_service,
)


def _is_alias(value: str) -> bool:
    return value.startswith("ALIAS_")


def _load_catalog_if_configured(state: RuntimeState) -> dict[str, int]:
    if _is_alias(state.settings.cards_json_path) or _is_alias(state.settings.assets_base_dir):
        logger.warning(
            "Catalog seeding skipped because CARDS_JSON_PATH or ASSETS_BASE_DIR still uses ALIAS_."
        )
        return {"kanji": 0, "cards": 0}

    catalog = load_catalog(state.settings.cards_json_file, state.settings.assets_root)
    with session_scope(state.session_factory) as session:
        seed_catalog(session, catalog)
        kanji_count = session.scalar(select(func.count()).select_from(Kanji))
        card_count = session.scalar(select(func.count()).select_from(KanjiCard))

    return {"kanji": int(kanji_count or 0), "cards": int(card_count or 0)}


async def require_admin_key(
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> None:
    runtime: RuntimeState = request.app.state.runtime
    if _is_alias(runtime.settings.admin_api_key):
        raise HTTPException(status_code=400, detail="ADMIN_API_KEY is still ALIAS_ value")
    if x_admin_key != runtime.settings.admin_api_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"ok": False, "error": "internal_server_error"})


@app.on_event("startup")
async def on_startup() -> None:
    runtime: RuntimeState = app.state.runtime

    try:
        counts = _load_catalog_if_configured(runtime)
        logger.info("Catalog ready: kanji=%s cards=%s", counts["kanji"], counts["cards"])
    except CatalogError as exc:
        logger.exception("Catalog load failed: %s", exc)

    await runtime.telegram.initialize()
    runtime.scheduler.start()
    logger.info("Application startup completed")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    runtime: RuntimeState = app.state.runtime
    runtime.scheduler.shutdown()
    await runtime.telegram.shutdown()
    logger.info("Application shutdown completed")


@app.get("/health")
async def health() -> dict:
    runtime: RuntimeState = app.state.runtime
    return {
        "ok": True,
        "app": runtime.settings.app_name,
        "env": runtime.settings.app_env,
        "bot_enabled": runtime.telegram.enabled,
        "telegram_mode": "webhook" if runtime.settings.telegram_use_webhook else "polling",
    }


@app.get("/health/deep")
async def health_deep() -> dict:
    runtime: RuntimeState = app.state.runtime
    with session_scope(runtime.session_factory) as session:
        kanji_count = session.scalar(select(func.count()).select_from(Kanji))
        card_count = session.scalar(select(func.count()).select_from(KanjiCard))

    return {
        "ok": True,
        "kanji_count": int(kanji_count or 0),
        "card_count": int(card_count or 0),
        "scheduler_running": bool(runtime.scheduler.scheduler and runtime.scheduler.scheduler.running),
    }


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request) -> dict:
    runtime: RuntimeState = app.state.runtime
    if not runtime.telegram.enabled:
        raise HTTPException(status_code=503, detail="Telegram bot is disabled")
    if not runtime.settings.telegram_use_webhook:
        raise HTTPException(status_code=400, detail="Webhook endpoint is disabled because TELEGRAM_USE_WEBHOOK=false")

    expected = runtime.settings.telegram_webhook_secret
    if _is_alias(expected):
        raise HTTPException(status_code=400, detail="TELEGRAM_WEBHOOK_SECRET is still ALIAS_ value")

    if secret != expected:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    payload = await request.json()
    await runtime.telegram.process_update(payload)
    return {"ok": True}


@app.post("/admin/telegram/set-webhook")
async def admin_set_webhook(request: Request, _: None = Depends(require_admin_key)) -> dict:
    runtime: RuntimeState = request.app.state.runtime
    if not runtime.settings.telegram_use_webhook:
        raise HTTPException(status_code=400, detail="Cannot set webhook when TELEGRAM_USE_WEBHOOK=false")
    url = await runtime.telegram.ensure_webhook()
    return {"ok": True, "webhook_url": url}


@app.post("/admin/telegram/delete-webhook")
async def admin_delete_webhook(request: Request, _: None = Depends(require_admin_key)) -> dict:
    runtime: RuntimeState = request.app.state.runtime
    if not runtime.settings.telegram_use_webhook:
        raise HTTPException(status_code=400, detail="Webhook mode is disabled because TELEGRAM_USE_WEBHOOK=false")
    await runtime.telegram.remove_webhook()
    return {"ok": True}


@app.post("/admin/jobs/run/{job_name}")
async def admin_run_job(job_name: str, request: Request, _: None = Depends(require_admin_key)) -> dict:
    runtime: RuntimeState = request.app.state.runtime
    if job_name == "morning":
        await runtime.scheduler.run_morning_job()
    elif job_name == "noon":
        await runtime.scheduler.run_noon_job()
    elif job_name == "evening":
        await runtime.scheduler.run_evening_job()
    elif job_name == "maintenance":
        await runtime.scheduler.run_maintenance_job()
    else:
        raise HTTPException(status_code=404, detail="Unknown job")

    return {"ok": True, "job": job_name}


@app.post("/admin/catalog/reseed")
async def admin_catalog_reseed(request: Request, _: None = Depends(require_admin_key)) -> dict:
    runtime: RuntimeState = request.app.state.runtime
    counts = _load_catalog_if_configured(runtime)
    return {"ok": True, "counts": counts}
