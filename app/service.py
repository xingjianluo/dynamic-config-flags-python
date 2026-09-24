"""FastAPI 服务装配：配置加载、开关判定与运行时管理接口。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from app.errors import ConfigRejectedError, StaleVersionError, UnknownVersionError
from app.flags import evaluate_flag
from app.merge import merge_layers
from app.schema import FLAG_SPECS
from app.sources import SourceLayer, load_defaults, load_env, load_file
from app.store import ConfigStore


def create_store(config_path: str | None = None) -> ConfigStore:
    """按优先级加载各层配置并构建存储：DEFAULTS < FILE < ENV。"""
    config = merge_layers(
        [
            (SourceLayer.DEFAULTS, load_defaults()),
            (SourceLayer.FILE, load_file(config_path)),
            (SourceLayer.ENV, load_env()),
        ]
    )
    flags = {name: rule.to_dict() for name, rule in FLAG_SPECS.items()}
    return ConfigStore(config, flags)


class UpdateRequest(BaseModel):
    config: dict[str, Any] | None = None
    flags: dict[str, Any] | None = None
    expected_version: int


class RollbackRequest(BaseModel):
    version: int


def _snapshot_view(snap) -> dict[str, Any]:
    return {"version": snap.version, "config": snap.config, "flags": snap.flags}


def create_app(store: ConfigStore | None = None) -> FastAPI:
    """创建 HTTP 服务。"""
    store = store or create_store()
    app = FastAPI(title="dynamic-config-flags")

    @app.get("/")
    def root() -> dict[str, str]:
        return {"Hello": "World"}

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        return _snapshot_view(store.snapshot())

    @app.post("/config")
    def update_config(req: UpdateRequest) -> dict[str, Any]:
        try:
            snap = store.update(req.config, req.flags, expected_version=req.expected_version)
        except StaleVersionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ConfigRejectedError as exc:
            raise HTTPException(
                status_code=422,
                detail={"reason": exc.reason.value, "keys": exc.keys, "detail": exc.detail},
            ) from exc
        return _snapshot_view(snap)

    @app.post("/config/rollback")
    def rollback(req: RollbackRequest) -> dict[str, Any]:
        try:
            snap = store.rollback(req.version)
        except UnknownVersionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _snapshot_view(snap)

    @app.get("/config/history")
    def history() -> dict[str, list[int]]:
        return {"versions": store.history()}

    @app.get("/flags/{flag}")
    def get_flag(flag: str, subject: str = Query(...)) -> dict[str, Any]:
        snap = store.snapshot()
        try:
            decision = evaluate_flag(flag, subject, snap.flags)
        except ConfigRejectedError as exc:
            raise HTTPException(
                status_code=404,
                detail={"reason": exc.reason.value, "keys": exc.keys},
            ) from exc
        return {
            "flag": decision.flag,
            "subject": decision.subject,
            "enabled": decision.enabled,
            "reason": decision.reason,
            "bucket": decision.bucket,
            "version": snap.version,
        }

    return app
