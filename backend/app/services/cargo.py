"""货物登记领域服务：货票登记、补报称重、单票确认装载与船舶余量校验。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.cross_module import check_cargo_tonnage, voyage_not_completed
from app.services.tonnage import (
    cargo_vessel,
    ensure_vessel_capacity,
    number,
    resolve_vessel,
    text,
    vessel_dwt,
)


class CargoService(BaseModuleService):
    MODULE = "cargo"
    ENTITY = "货物"
    OPERATION_TERM = "确认装载"
    LIST_FIELDS = [
        "货票编号", "货物品名", "托运方", "件数", "总吨数", "复磅吨数",
        "舱位", "装载船舶", "装载航次", "装货港", "卸货港", "装载批次号", "备注说明",
    ]
    REQUIRED_FIELDS = ["货物品名", "托运方", "件数"]
    STATUS_ORDER = ["草稿", "待装载", "已装载", "已卸货"]
    ACTION_RULES = {"调整数量": "待装载", "确认装载": "已装载", "确认卸货": "已卸货"}
    NEGATIVE_ACTIONS = []
    # 允许附值动作：调整数量需要带上新的复磅吨数。
    ACCEPTS_VALUES = ["调整数量"]

    def run_action_with_values(
        self, entry_id: int, action: str, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        if action in {"调整数量", "补报称重"}:
            return self.reweigh(entry_id, values)
        return self.run_action(entry_id, action)

    def create_entry(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]] | tuple[dict[str, Any] | None, str]:
        # 跨模块校验：装载航次必须存在且未完成
        voyage_id = values.get("装载航次", "")
        if voyage_id:
            ok, msg = voyage_not_completed(voyage_id)
            if not ok:
                return None, f"货物登记失败：{msg}"

        # 跨模块校验：直接指定船舶时，按船舶已占用吨位 + 本票吨位核对余量
        vessel = resolve_vessel(values.get("装载船舶")) if values.get("装载船舶") else None
        tonnage = number(values.get("总吨数", 0))
        if vessel is not None:
            ensure_vessel_capacity(vessel)
            dwt = vessel_dwt(vessel)
            occupied = number(vessel.get("已占用吨位"))
            if tonnage > 0 and round(occupied + tonnage, 2) > dwt:
                return None, (
                    f"货物登记失败：装载后船舶占用 {round(occupied + tonnage, 2)} 吨"
                    f"超过载重吨 {dwt} 吨"
                )
        elif tonnage > 0 and voyage_id:
            ok, msg = check_cargo_tonnage(voyage_id, tonnage)
            if not ok:
                return None, f"货物登记失败：{msg}"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["货票编号"] = text(values.get("货票编号")) or f"CGO-{int(entry['id']):04d}"
        entry["总吨数"] = tonnage
        if values.get("复磅吨数") not in (None, ""):
            entry["复磅吨数"] = number(values.get("复磅吨数"))
        if vessel is not None:
            entry["装载船舶"] = text(vessel.get("船名")) or str(vessel.get("id"))
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        # 确认装载统一走配载批次回执：单票确认也会生成一张回执并联动船舶余量。
        if action == "确认装载":
            from app.services.stowage import stowage

            cargo, message = stowage.confirm_cargo(entry_id)
            return cargo, message
        if action == "确认卸货":
            from app.services.stowage import stowage

            cargo, message = stowage.release_cargo(entry_id)
            return cargo, message
        if action == "调整数量":
            return None, (
                "调整数量需要带上复磅吨数，请使用「补报称重」并填写新吨数；"
                "未带吨数的空调整不会改变货票状态，避免绕过配载回执"
            )
        return super().run_action(entry_id, action)

    def reweigh(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        from app.services.stowage import stowage

        return stowage.reweigh(entry_id, values)

    def eligibility(self, entry_id: int) -> dict[str, Any]:
        from app.services.stowage import stowage

        return stowage.eligibility(entry_id)
