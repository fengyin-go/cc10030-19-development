"""配载批次回执接口：按舱位批量核对、多选提交、单票确认与船舶余量查询。

这些路径挂在 /api/stowage 下，和通用资源路由 /api/{module} 区分开，
避免把业务动作伪装成某个模块的字段查询。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.services.stowage import stowage
from app.services.tonnage import (
    cargo_eligibility,
    ensure_vessel_capacity,
    number,
    resolve_cargo,
    resolve_vessel,
    vessel_capacity,
)

router = APIRouter(prefix="/api/stowage", tags=["配载批次"])


class SelectionPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    vessel: Any = Field(default=None, alias="船舶档案")
    tickets: Any = Field(default=None, alias="货票")
    slot: str | None = Field(default=None, alias="舱位")
    remark: str | None = Field(default=None, alias="备注说明")
    batch_no: str | None = Field(default=None, alias="批次号")
    confirm_now: bool = Field(default=False, alias="立即确认")

    def selection_values(self) -> dict[str, Any]:
        return {
            "船舶档案": self.vessel,
            "货票清单": self.tickets,
            "舱位": self.slot,
            "备注说明": self.remark,
            "批次号": self.batch_no,
        }


class BatchPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    vessel: Any = Field(default=None, alias="船舶档案")
    tickets: Any = Field(default=None, alias="货票清单")
    slot: str | None = Field(default=None, alias="舱位")
    remark: str | None = Field(default=None, alias="备注说明")
    batch_no: str | None = Field(default=None, alias="批次号")
    confirm_now: bool = Field(default=False, alias="立即确认")

    def values(self) -> dict[str, Any]:
        return {
            "船舶档案": self.vessel,
            "货票清单": self.tickets,
            "舱位": self.slot,
            "备注说明": self.remark,
            "批次号": self.batch_no,
        }


class RemarkPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    remark: str | None = Field(default=None, alias="备注说明")


class ReweighPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    weight: Any = Field(default=None, alias="复磅吨数")
    remark: str | None = Field(default=None, alias="备注说明")

    def values(self) -> dict[str, Any]:
        return {"复磅吨数": self.weight, "备注说明": self.remark}


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    batches = stowage.list_batches()
    return {"items": batches, "total": len(batches)}


@router.post("/preview")
def preview(payload: SelectionPayload) -> dict[str, Any]:
    return stowage.preview(
        vessel_ref=payload.vessel,
        selections=payload.tickets,
        default_slot=payload.slot or "",
    )


@router.post("/batches")
def create_batch(payload: BatchPayload) -> dict[str, Any]:
    entry, message = stowage.submit_batch(payload.values(), auto_confirm=payload.confirm_now)
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int) -> dict[str, Any]:
    batch = stowage.get_entry(batch_id)
    if batch is None:
        return {"ok": False, "message": f"配载批次回执 {batch_id} 不存在", "entry": None}
    return {"ok": True, "message": "", "entry": stowage.enrich(batch)}


@router.post("/batches/{batch_id}/confirm")
def confirm_batch(batch_id: int) -> dict[str, Any]:
    entry, message = stowage.confirm_batch(batch_id)
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.post("/batches/{batch_id}/return")
def return_batch(batch_id: int, payload: RemarkPayload | None = None) -> dict[str, Any]:
    entry, message = stowage.return_batch(batch_id, (payload.remark if payload else "") or "")
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.get("/cargo/{cargo_ref}")
def cargo_status(cargo_ref: str) -> dict[str, Any]:
    cargo = resolve_cargo(cargo_ref)
    if cargo is None:
        return {"ok": False, "说明": f"货票 {cargo_ref} 不存在"}
    eligibility = stowage.eligibility(cargo_ref)
    eligibility["批次回执"] = stowage.batches_for_cargo(cargo_ref)
    return eligibility


@router.post("/cargo/{cargo_ref}/confirm")
def confirm_cargo(cargo_ref: str, payload: RemarkPayload | None = None) -> dict[str, Any]:
    cargo = resolve_cargo(cargo_ref)
    if cargo is None:
        return {"ok": False, "message": f"货票 {cargo_ref} 不存在", "entry": None}
    entry, message = stowage.confirm_cargo(
        int(cargo["id"]), (payload.remark if payload else "") or ""
    )
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.post("/cargo/{cargo_ref}/reweigh")
def reweigh_cargo(cargo_ref: str, payload: ReweighPayload) -> dict[str, Any]:
    cargo = resolve_cargo(cargo_ref)
    if cargo is None:
        return {"ok": False, "message": f"货票 {cargo_ref} 不存在", "entry": None}
    entry, message = stowage.reweigh(int(cargo["id"]), payload.values())
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.get("/vessels/{vessel_ref}")
def vessel_capacity_view(vessel_ref: str) -> dict[str, Any]:
    vessel = resolve_vessel(vessel_ref)
    if vessel is None:
        return {"ok": False, "message": f"船舶 {vessel_ref} 不存在"}
    ensure_vessel_capacity(vessel)
    result = {"ok": True, **vessel_capacity(vessel)}
    result["回执"] = [
        stowage.enrich(batch)
        for batch in stowage.list_batches()
        if int(batch.get("船舶档案编号", 0) or 0) == int(vessel.get("id", 0))
    ]
    return result
