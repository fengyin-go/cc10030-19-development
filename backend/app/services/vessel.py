"""船舶档案领域服务：跨模块业务链路（船舶→航次→封存级联）与配载吨位回写。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.cross_module import seal_vessel_cascade
from app.services.tonnage import ensure_vessel_capacity, number, vessel_dwt


class VesselService(BaseModuleService):
    MODULE = "vessel"
    ENTITY = "船舶"
    OPERATION_TERM = "确认档案"
    LIST_FIELDS = [
        "船名", "总吨位", "载重吨", "已占用吨位", "剩余载重吨",
        "建造年份", "船籍港", "船长姓名", "当前状态", "备注说明",
    ]
    REQUIRED_FIELDS = ["船名", "总吨位", "载重吨"]
    STATUS_ORDER = ["草稿", "待确认", "已确认", "已封存"]
    ACTION_RULES = {"确认档案": "已确认", "更新状态": "已确认", "封存船舶": "已封存"}
    NEGATIVE_ACTIONS = []

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        # 列表返回前先按载重吨补齐占用/余量，旧数据也能显示真实余量。
        for row in self._rows():
            ensure_vessel_capacity(row)
        return super().list_entries(**kwargs)

    def _rows(self) -> list[dict[str, Any]]:
        from app.store import store

        return store.rows(self.MODULE)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = super().get_entry(entry_id)
        if entry is not None:
            ensure_vessel_capacity(entry)
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is not None:
            entry["载重吨"] = number(entry.get("载重吨"))
            entry["已占用吨位"] = 0.0
            entry["剩余载重吨"] = round(vessel_dwt(entry), 2)
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        # 封存船舶时级联检查所有进行中航次
        if action == "封存船舶":
            ok, msg = seal_vessel_cascade(entry_id)
            if not ok:
                return None, f"封存失败：{msg}"
        return super().run_action(entry_id, action)
