"""动态配置与功能开关服务。"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.loader import load_bootstrap
from app.schema import ConfigRejected, RejectReason
from app.store import ConfigStore

logger = logging.getLogger("dynamic_config")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# 启动时分层加载（默认值 < 文件 < 环境变量），非法配置直接启动失败
_bootstrap, _provenance = load_bootstrap(os.environ.get("DYNAMIC_CONFIG_FILE"))
store = ConfigStore(_bootstrap)
logger.info("启动加载完成: 配置=%s 来源=%s", _bootstrap, _provenance)

app = FastAPI(title="dynamic-config-flags")


class UpdateRequest(BaseModel):
    expected_version: int = Field(..., description="调用方读取到的当前版本，乐观锁")
    config: dict


class RollbackRequest(BaseModel):
    target_version: int


@app.get("/config")
def get_config() -> dict:
    """查看当前生效配置、版本与键级来源。"""
    return {
        "version": store.version,
        "config": store.get_config(),
        "provenance": _provenance,
        "versions": store.history_versions(),
    }


@app.post("/config")
def update_config(req: UpdateRequest) -> dict:
    """携带 expected_version 的整份写入；过期版本或非法配置将被拒绝。"""
    logger.info("收到更新: expected_version=%s 输入配置=%s", req.expected_version, req.config)
    try:
        new_version = store.update(req.config, req.expected_version)
    except ConfigRejected as exc:
        logger.warning("更新被拒绝: reason=%s detail=%s", exc.reason.value, exc.detail)
        raise HTTPException(status_code=409, detail={"reason": exc.reason.value, "detail": exc.detail})
    logger.info("更新生效: 新版本=%s", new_version)
    return {"version": new_version, "config": store.get_config()}


@app.post("/config/rollback")
def rollback_config(req: RollbackRequest) -> dict:
    """回滚到历史版本（生成新版本号，内容与目标版本一致）。"""
    try:
        new_version = store.rollback(req.target_version)
    except ConfigRejected as exc:
        raise HTTPException(status_code=404, detail={"reason": exc.reason.value, "detail": exc.detail})
    logger.info("回滚生效: target=%s 新版本=%s", req.target_version, new_version)
    return {"version": new_version, "config": store.get_config()}


@app.get("/flags/{flag}")
def check_flag(flag: str, subject: str) -> dict:
    """按主体判定功能开关，返回结论与可解释依据。"""
    decision = store.check_flag(flag, subject)
    logger.info("开关判定: flag=%s subject=%s 结论=%s 依据=%s",
                flag, subject, decision.allowed, decision.basis)
    return asdict(decision)
