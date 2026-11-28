"""配载批次回执领域服务：按舱位批量核对装载，并保证货票和船舶余量的一致性。

核心约束：
- 配载批次回执必须关联一条船舶档案，确认后按每张货票的实际吨位回写船舶占用；
- 同一货票在任一时刻只能出现在一份「有效」批次里（已确认 / 部分确认）；
- 每次确认都先算余量再装载，任何一次有效确认后船舶占用都不超过载重吨；
- 核对未通过的货票不改状态、不扣吨位，仍留在原批回执里可以再办理。
"""
from __future__ import annotations

import json
import threading
from typing import Any, Iterable

from app.services.base import BaseModuleService
from app.services.tonnage import (
    CONFIRMED,
    LOADED,
    PARTIAL,
    RETURNED,
    UNLOADED,
    WAITING,
    VALID_BATCH_STATUSES,
    cargo_eligibility,
    cargo_tonnage,
    cargo_ticket,
    cargo_vessel,
    ensure_vessel_capacity,
    find_loaded_batch,
    number,
    resolve_cargo,
    resolve_vessel,
    text,
    vessel_capacity,
    vessel_dwt,
)
from app.store import store

# 提交、核对和确认共用一把可重入锁：FastAPI 的同步接口跑在线程池里，
# 没有这把锁，两份并发请求会同时读到「货票仍待装载」，然后各自扣一次吨位。
STOWAGE_LOCK = threading.RLock()

LOADED_RESULTS = {LOADED}
VALID_RESULTS = {LOADED, UNLOADED}
CONFLICT = "待原批协调"
CAPACITY_SHORT = "余量不足"
INVALID = "不可装载"
RETURNED_RESULT = "已退回待协调"


class StowageService(BaseModuleService):
    MODULE = "stowage_batch"
    ENTITY = "配载批次回执"
    LIST_FIELDS = [
        "批次号", "船舶档案", "舱位清单", "货票清单", "批次明细",
        "核准吨位", "船舶载重吨", "确认前占用", "确认后占用", "剩余载重吨", "备注说明",
    ]
    REQUIRED_FIELDS = ["船舶档案", "货票清单"]
    STATUS_ORDER = [WAITING, CONFIRMED, PARTIAL, RETURNED]
    ACTION_RULES = {
        "核对装载": CONFIRMED,
        "确认配载": CONFIRMED,
        "退回协调": RETURNED,
    }
    NEGATIVE_ACTIONS = ["退回协调"]

    # ---------------------------------------------------------------- 批次建立

    def create_entry(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]] | tuple[dict[str, Any] | None, str]:
        with STOWAGE_LOCK:
            return self._create_batch(values)

    def _create_batch(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | list[str]]:
        vessel = resolve_vessel(
            values.get("船舶档案") or values.get("船舶编号") or values.get("船名")
        )
        if vessel is None:
            return None, f"配载批次回执必须关联有效的船舶档案：{values.get('船舶档案') or values.get('船舶编号') or ''}"

        raw_selection = (
            values.get("货票清单")
            or values.get("货票")
            or values.get("货票编号")
            or values.get("tickets")
        )
        selection = self._normalize_selection(raw_selection, text(values.get("舱位") or values.get("舱位清单")))
        if not selection:
            return None, ["货票清单"]

        details: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in selection:
            cargo = self._resolve_selection_cargo(item)
            if cargo is None:
                return None, f"货票「{item.get('票号') or item.get('cargo_id') or ''}」不存在"
            cargo_id = int(cargo.get("id", 0))
            if cargo_id in seen:
                return None, f"同一批次内货票 {cargo_ticket(cargo)} 重复选择"
            seen.add(cargo_id)
            cargo_vessel_row = cargo_vessel(cargo)
            if cargo_vessel_row is not None and int(cargo_vessel_row.get("id", 0)) != int(vessel.get("id", 0)):
                return None, (
                    f"货票 {cargo_ticket(cargo)} 归属船舶 {cargo_vessel_row.get('船名')}，"
                    f"与本批次船舶 {vessel.get('船名')} 不一致"
                )

            slot = text(item.get("舱位")) or text(cargo.get("舱位")) or "未分舱"
            details.append({
                "cargo_id": cargo_id,
                "货票编号": cargo_ticket(cargo),
                "舱位": slot,
                "申报吨位": cargo_tonnage(cargo),
                "result": WAITING,
                "确认吨位": 0.0,
                "说明": "已进入配载批次，等待船舶调度核对余量",
            })

        slots = sorted({detail["舱位"] for detail in details})
        batch_no = text(values.get("批次号")) or self._next_batch_no()
        rows = store.rows(self.MODULE)
        if any(text(row.get("批次号")) == batch_no for row in rows):
            return None, f"批次号 {batch_no} 已存在，请更换"
        entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1

        ensure_vessel_capacity(vessel)
        entry: dict[str, Any] = {
            "id": entry_id,
            "status": WAITING,
            "pending": True,
            "abnormal": False,
            "批次号": batch_no,
            "船舶档案": text(vessel.get("船名")) or str(vessel.get("id")),
            "船舶档案编号": vessel.get("id"),
            "舱位清单": "、".join(slots),
            "货票清单": "、".join(detail["货票编号"] for detail in details),
            "批次明细": details,
            "核准吨位": 0.0,
            "船舶载重吨": vessel_dwt(vessel),
            "确认前占用": number(vessel.get("已占用吨位")),
            "确认后占用": number(vessel.get("已占用吨位")),
            "剩余载重吨": number(vessel.get("剩余载重吨")),
            "备注说明": text(values.get("备注说明")),
        }
        rows.append(entry)
        return entry, []

    def _next_batch_no(self) -> str:
        existing = {text(row.get("批次号")) for row in store.rows(self.MODULE)}
        index = len(existing) + 1
        while f"STW-{index:04d}" in existing:
            index += 1
        return f"STW-{index:04d}"

    # ---------------------------------------------------------------- 选择解析

    def _normalize_selection(self, raw: Any, default_slot: str = "") -> list[dict[str, Any]]:
        """接受 [id/票号]、{舱位: [票号]}、[{cargo_id, 舱位}] 和逗号分隔文本。"""
        items: list[dict[str, Any]] = []

        def push(value: Any, slot: str = "") -> None:
            if isinstance(value, dict):
                cargo_id = value.get("cargo_id") or value.get("id")
                ticket = (
                    value.get("货票编号") or value.get("票号") or value.get("ticket")
                    or value.get("货票") or value.get("cargo")
                )
                if cargo_id is None and ticket is None:
                    return
                items.append({
                    "cargo_id": cargo_id,
                    "票号": ticket,
                    "舱位": text(value.get("舱位") or value.get("slot") or slot),
                })
            elif value is not None:
                items.append({"cargo_id": None, "票号": value, "舱位": slot})

        if isinstance(raw, str):
            content = raw.strip()
            if not content:
                return []
            if content.startswith("[") or content.startswith("{"):
                try:
                    raw = json.loads(content)
                except ValueError:
                    raw = content
            else:
                raw = [part for part in content.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",").split(",") if part.strip()]
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                push(item, default_slot)
        elif isinstance(raw, dict):
            item_keys = {"cargo_id", "id", "货票编号", "票号", "ticket", "货票", "cargo"}
            if item_keys & set(raw):
                push(raw, default_slot)
            else:
                for slot, tickets in raw.items():
                    if isinstance(tickets, (list, tuple, set)):
                        for ticket in tickets:
                            push(ticket, text(slot) or default_slot)
                    else:
                        push(tickets, text(slot) or default_slot)
        elif raw is not None:
            push(raw, default_slot)
        return items

    def _resolve_selection_cargo(self, item: dict[str, Any]) -> dict[str, Any] | None:
        cargo_id = item.get("cargo_id")
        if cargo_id not in (None, ""):
            try:
                return store.find("cargo", int(cargo_id))
            except (TypeError, ValueError):
                return None
        ticket = item.get("票号")
        return resolve_cargo(ticket)

    # ---------------------------------------------------------------- 批次核对

    def preview(
        self,
        *,
        vessel_ref: Any = None,
        selections: Any = None,
        batch_id: int | None = None,
        default_slot: str = "",
    ) -> dict[str, Any]:
        """余量预检：只读，不修改货票、船舶和批次回执。"""
        with STOWAGE_LOCK:
            batch = self.get_entry(int(batch_id)) if batch_id is not None else None
            if batch_id is not None and batch is None:
                return {"ok": False, "说明": f"配载批次回执 {batch_id} 不存在"}
            if batch is not None:
                vessel = resolve_vessel(batch.get("船舶档案") or batch.get("船舶档案编号"))
                selection_items = [
                    {
                        "cargo_id": detail.get("cargo_id"),
                        "票号": detail.get("货票编号"),
                        "舱位": detail.get("舱位"),
                    }
                    for detail in batch.get("批次明细") or []
                ]
            else:
                vessel = resolve_vessel(
                    vessel_ref or selections.get("船舶档案") if isinstance(selections, dict) else vessel_ref
                )
                raw = selections
                if isinstance(selections, dict):
                    raw = selections.get("货票清单") or selections.get("货票") or selections.get("货票编号")
                selection_items = self._normalize_selection(raw, default_slot)
            if vessel is None:
                return {"ok": False, "说明": "必须指定有效的船舶档案才能核对配载余量"}
            if not selection_items:
                return {"ok": False, "说明": "至少选择一张货票才能核对配载余量"}

            ensure_vessel_capacity(vessel)
            dwt = vessel_dwt(vessel)
            occupied = number(vessel.get("已占用吨位"))
            available = round(dwt - occupied, 2)

            details: list[dict[str, Any]] = []
            already_loaded: list[str] = []
            pending_loads = 0.0
            for item in selection_items:
                cargo = self._resolve_selection_cargo(item)
                if cargo is None:
                    details.append({
                        "cargo_id": item.get("cargo_id"),
                        "货票编号": text(item.get("票号")),
                        "舱位": text(item.get("舱位")) or default_slot or "未分舱",
                        "申报吨位": 0.0,
                        "可确认": False,
                        "单票可确认": False,
                        "结论": INVALID,
                        "说明": "货票不存在",
                    })
                    continue

                slot = text(item.get("舱位")) or text(cargo.get("舱位")) or default_slot or "未分舱"
                actual = cargo_tonnage(cargo)
                cargo_vessel_row = cargo_vessel(cargo)
                if (
                    cargo_vessel_row is not None
                    and int(cargo_vessel_row.get("id", 0)) != int(vessel.get("id", 0))
                ):
                    details.append(self._preview_item(
                        cargo, slot, False, False, INVALID,
                        f"货票归属船舶 {cargo_vessel_row.get('船名')}，与本批次船舶不一致",
                    ))
                    continue

                # 本批次此前已经实际装载的货票：只展示，不重复占吨位。
                already_in_batch = False
                if batch is not None:
                    for detail in batch.get("批次明细") or []:
                        if (
                            int(detail.get("cargo_id", 0)) == int(cargo.get("id", 0))
                            and text(detail.get("result")) == LOADED
                        ):
                            already_in_batch = True
                            break
                if already_in_batch:
                    already_loaded.append(cargo_ticket(cargo))
                    details.append(self._preview_item(
                        cargo, slot, False, False, LOADED,
                        "本批次此前已实际装载，吨位已计入船舶占用，不再重复扣减",
                    ))
                    continue

                other_batch, _ = find_loaded_batch(
                    int(cargo.get("id", 0)), exclude_batch_id=int(batch["id"]) if batch else None
                )
                if other_batch is not None:
                    details.append(self._preview_item(
                        cargo, slot, False, False, CONFLICT,
                        f"货票已在配载批次 {other_batch.get('批次号')} 实际装载，只能在其中一份有效批次生效；"
                        "如要改配，需先由原批退回协调",
                    ))
                    continue
                if text(cargo.get("status")) == LOADED:
                    details.append(self._preview_item(
                        cargo, slot, False, False, CONFLICT,
                        "货票已处于「已装载」，不能重复确认",
                    ))
                    continue

                individually = cargo_eligibility(cargo, vessel=vessel, occupied=occupied)
                batch_ok = bool(individually.get("eligible")) and actual <= available
                if batch_ok:
                    available = round(available - actual, 2)
                    pending_loads = round(pending_loads + actual, 2)
                    note = (
                        f"可确认，装载后船舶占用 {round(occupied + pending_loads, 2)}/{dwt} 吨，"
                        f"剩余 {available} 吨"
                    )
                    result = "可确认"
                elif individually.get("eligible") and actual > available:
                    result = CAPACITY_SHORT
                    note = (
                        f"这张票单独按 {actual} 吨可确认，但本批按舱位累计后只剩 {available} 吨；"
                        "本批可先确认前序货票，其余等余量释放或拆批提交"
                    )
                else:
                    result = CONFLICT if individually.get("requires_coordination") else INVALID
                    note = individually.get("reason") or "当前不可确认"
                details.append(self._preview_item(
                    cargo, slot, batch_ok, bool(individually.get("eligible")), result, note,
                ))

            total_after = round(occupied + pending_loads, 2)
            if total_after > dwt:
                return {"ok": False, "说明": f"确认后占用 {total_after} 吨超过载重吨 {dwt} 吨"}
            batch_tonnage = round(pending_loads, 2)
            batch_ok_list = [item["货票编号"] for item in details if item.get("可确认")]
            solo = [item["货票编号"] for item in details if item.get("单票可确认")]
            cooperation = [
                item["货票编号"] for item in details if item.get("结论") == CONFLICT
            ]
            capacity_short = [
                item["货票编号"] for item in details if item.get("结论") == CAPACITY_SHORT
            ]
            return {
                "ok": True,
                "船舶档案": text(vessel.get("船名")) or str(vessel.get("id")),
                "船舶档案编号": vessel.get("id"),
                "载重吨": dwt,
                "已占用吨位": occupied,
                "剩余载重吨": round(dwt - occupied, 2),
                "本次可确认吨位": batch_tonnage,
                "确认后占用": total_after,
                "确认后剩余": round(dwt - total_after, 2),
                "明细": details,
                "可本批确认货票": batch_ok_list,
                "本批已装载货票": already_loaded,
                "单票可确认货票": solo,
                "需协调货票": cooperation,
                "余量不足货票": capacity_short,
                "说明": (
                    "本批货票均可确认" if all(item.get("可确认") for item in details)
                    else "部分货票需回原批协调或等待船舶余量"
                ),
            }

    def _preview_item(
        self,
        cargo: dict[str, Any],
        slot: str,
        batch_ok: bool,
        solo_ok: bool,
        conclusion: str,
        note: str,
    ) -> dict[str, Any]:
        return {
            "cargo_id": cargo.get("id"),
            "货票编号": cargo_ticket(cargo),
            "舱位": slot,
            "申报吨位": cargo_tonnage(cargo),
            "可确认": batch_ok,
            "单票可确认": solo_ok,
            "结论": conclusion,
            "说明": note,
        }

    # ---------------------------------------------------------------- 批次确认

    def confirm_batch(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        with STOWAGE_LOCK:
            batch = self.get_entry(int(batch_id))
            if batch is None:
                return None, f"配载批次回执 {batch_id} 不存在"
            if text(batch.get("status")) == CONFIRMED and all(
                text(detail.get("result")) in VALID_RESULTS
                for detail in batch.get("批次明细") or []
            ):
                return batch, f"批次 {batch.get('批次号')} 已确认，无需重复操作"

            vessel = resolve_vessel(batch.get("船舶档案") or batch.get("船舶档案编号"))
            if vessel is None:
                return None, f"批次 {batch.get('批次号')} 关联的船舶档案已不存在"
            ensure_vessel_capacity(vessel)
            dwt = vessel_dwt(vessel)
            occupied_before = number(vessel.get("已占用吨位"))
            available = round(dwt - occupied_before, 2)

            details = batch.get("批次明细") or []
            # 部分确认的批次：此前已装载的货票继续计入现有占用，不能重复加。
            previous_loaded = 0.0
            for detail in details:
                if text(detail.get("result")) == LOADED:
                    previous_loaded = round(previous_loaded + number(detail.get("确认吨位")), 2)

            new_loaded = 0.0
            conflicts: list[str] = []
            for detail in details:
                if text(detail.get("result")) == LOADED:
                    continue
                cargo = store.find("cargo", int(detail.get("cargo_id", 0)))
                if cargo is None:
                    detail["result"] = INVALID
                    detail["说明"] = "货票已归档或不存在"
                    continue
                other_batch, _ = find_loaded_batch(
                    int(cargo.get("id", 0)), exclude_batch_id=int(batch["id"])
                )
                cargo_vessel_row = cargo_vessel(cargo)
                if (
                    cargo_vessel_row is not None
                    and int(cargo_vessel_row.get("id", 0)) != int(vessel.get("id", 0))
                ):
                    detail["result"] = INVALID
                    detail["说明"] = (
                        f"货票归属船舶 {cargo_vessel_row.get('船名')}，与本批次船舶不一致"
                    )
                    continue
                if other_batch is not None:
                    detail["result"] = CONFLICT
                    detail["说明"] = (
                        f"货票 {cargo_ticket(cargo)} 已在配载批次 {other_batch.get('批次号')} 实际装载，"
                        "需与原批协调，未在本批重复扣减吨位"
                    )
                    conflicts.append(cargo_ticket(cargo))
                    continue

                individual = cargo_eligibility(cargo, vessel=vessel, occupied=occupied_before + new_loaded)
                actual = cargo_tonnage(cargo)
                if not individual.get("eligible"):
                    conclusion = CONFLICT if individual.get("requires_coordination") else INVALID
                    detail["result"] = conclusion
                    detail["说明"] = individual.get("reason")
                    if conclusion == CONFLICT:
                        conflicts.append(cargo_ticket(cargo))
                    continue
                if actual > available:
                    detail["result"] = CAPACITY_SHORT
                    detail["说明"] = (
                        f"货票 {cargo_ticket(cargo)} 需 {actual} 吨，船舶剩余仅 {available} 吨；"
                        "货票保持待装载，可回原批或调整后再确认"
                    )
                    conflicts.append(cargo_ticket(cargo))
                    continue

                # 真正落货：状态、回执、船舶占用同时写入。
                cargo["status"] = LOADED
                cargo["pending"] = False
                cargo["abnormal"] = False
                cargo["装载船舶"] = text(vessel.get("船名")) or str(vessel.get("id"))
                cargo["装载批次号"] = batch.get("批次号")
                cargo["配载回执编号"] = batch.get("id")
                detail["result"] = LOADED
                detail["确认吨位"] = actual
                detail["申报吨位"] = actual
                detail["说明"] = (
                    f"已实际装载，船舶占用 {round(occupied_before + new_loaded + actual, 2)}/{dwt} 吨"
                )
                new_loaded = round(new_loaded + actual, 2)
                available = round(available - actual, 2)

            final_occupied = round(occupied_before + new_loaded, 2)
            if final_occupied > dwt:  # 兜底，正常路径不会到这里
                return None, f"确认失败：船舶占用 {final_occupied} 吨超过载重吨 {dwt} 吨"
            vessel["已占用吨位"] = final_occupied
            vessel["剩余载重吨"] = round(dwt - final_occupied, 2)

            all_loaded = all(text(item.get("result")) in VALID_RESULTS for item in details)
            any_loaded = any(text(item.get("result")) == LOADED for item in details)
            if all_loaded:
                status = CONFIRMED
            elif any_loaded:
                status = PARTIAL
            else:
                status = RETURNED
            batch["status"] = status
            batch["pending"] = status != CONFIRMED
            batch["abnormal"] = status != CONFIRMED
            batch["核准吨位"] = round(previous_loaded + new_loaded, 2)
            batch["确认后占用"] = final_occupied
            batch["剩余载重吨"] = round(dwt - final_occupied, 2)
            batch["未确认货票"] = [
                text(item.get("货票编号")) for item in details
                if text(item.get("result")) not in VALID_RESULTS
            ]
            if text(batch.get("备注说明")) and conflicts and status == PARTIAL:
                batch["备注说明"] = (
                    text(batch.get("备注说明")) + f"；需协调：{'、'.join(conflicts)}"
                )
            elif conflicts and not text(batch.get("备注说明")):
                batch["备注说明"] = f"需协调：{'、'.join(conflicts)}"

            if status == CONFIRMED:
                message = (
                    f"配载批次 {batch.get('批次号')} 已确认，装载 {batch.get('核准吨位')} 吨，"
                    f"船舶 {batch.get('船舶档案')} 剩余载重吨 {batch.get('剩余载重吨')} 吨"
                )
            elif status == PARTIAL:
                message = (
                    f"配载批次 {batch.get('批次号')} 部分确认，本次实际装载 {new_loaded} 吨，"
                    f"未确认货票仍可回原批办理：{'、'.join(batch['未确认货票']) or '无'}；"
                    f"船舶剩余载重吨 {batch.get('剩余载重吨')} 吨"
                )
            else:
                message = (
                    f"配载批次 {batch.get('批次号')} 核对未通过，货票状态和船舶吨位均未变更，"
                    "未确认货票可回原批办理"
                )
            return batch, message

    def submit_batch(self, values: dict[str, Any], auto_confirm: bool = False) -> tuple[dict[str, Any] | None, str]:
        """提交配载请求；auto_confirm=True 时等价于「提交并立即核对确认」。"""
        entry, result = self.create_entry(values)
        if entry is None:
            missing = result if isinstance(result, str) else f"缺少必填字段：{'、'.join(result)}"
            return None, missing
        if auto_confirm:
            return self.confirm_batch(int(entry["id"]))
        preview = self.preview(batch_id=int(entry["id"]))
        return entry, (
            f"配载批次回执 {entry.get('批次号')} 已生成，船舶剩余载重吨 "
            f"{preview.get('剩余载重吨', entry.get('剩余载重吨'))} 吨；{preview.get('说明', '')}"
        )

    def return_batch(self, batch_id: int, remark: str = "") -> tuple[dict[str, Any] | None, str]:
        with STOWAGE_LOCK:
            batch = self.get_entry(int(batch_id))
            if batch is None:
                return None, f"配载批次回执 {batch_id} 不存在"
            details = batch.get("批次明细") or []
            loaded = [item for item in details if text(item.get("result")) == LOADED]
            unconfirmed = [item for item in details if text(item.get("result")) not in VALID_RESULTS]
            for item in unconfirmed:
                item["result"] = RETURNED_RESULT
                item["说明"] = "未实际装载，保留待装载状态，可回到原批重新办理"
                item["可回原批"] = True
            if loaded:
                # 已装载的货票保留在有效批次里，不能因为退回而放开重复装载。
                batch["status"] = PARTIAL
                batch["pending"] = True
                batch["abnormal"] = True
                batch["未确认货票"] = [text(item.get("货票编号")) for item in unconfirmed]
                message = (
                    f"批次 {batch.get('批次号')} 中已装载货票保留在有效回执里，"
                    f"未确认货票 {('、'.join(batch['未确认货票']) or '无')} 可回原批办理"
                )
            else:
                batch["status"] = RETURNED
                batch["pending"] = True
                batch["abnormal"] = True
                batch["未确认货票"] = [text(item.get("货票编号")) for item in details]
                message = (
                    f"批次 {batch.get('批次号')} 已退回协调，全部货票未扣吨位，可回原批重新提交"
                )
            if remark:
                existing = text(batch.get("备注说明"))
                batch["备注说明"] = f"{existing}；{remark}" if existing else remark
            return batch, message

    # ---------------------------------------------------------------- 单票确认

    def confirm_cargo(self, cargo_id: int, remark: str = "") -> tuple[dict[str, Any] | None, str]:
        """单票「确认装载」也生成一张配载批次回执，便于调度核对余量。"""
        with STOWAGE_LOCK:
            cargo = store.find("cargo", int(cargo_id))
            if cargo is None:
                return None, f"货物 {cargo_id} 不存在或已归档"
            if text(cargo.get("status")) == LOADED:
                other_batch, _ = find_loaded_batch(int(cargo_id))
                if other_batch is not None:
                    return None, (
                        f"货物 {cargo_ticket(cargo)} 已在配载批次 {other_batch.get('批次号')} 实际装载，"
                        "需与原批协调，未重复扣减船舶吨位"
                    )
                return None, f"货物 {cargo_ticket(cargo)} 已处于「已装载」，不能重复确认"

            vessel = cargo_vessel(cargo)
            if vessel is None:
                return None, f"货物 {cargo_ticket(cargo)} 未关联有效船舶档案，无法生成配载批次回执"
            ensure_vessel_capacity(vessel)
            dwt = vessel_dwt(vessel)

            # 单票确认同样检查其他批次是否已经占用货票。
            other_batch, _ = find_loaded_batch(int(cargo_id))
            if other_batch is not None:
                return None, (
                    f"货物 {cargo_ticket(cargo)} 已在配载批次 {other_batch.get('批次号')} 实际装载，"
                    "A 类货票只能在一份有效批次中生效，需先与原批协调"
                )
            eligibility = cargo_eligibility(
                cargo, vessel=vessel, occupied=number(vessel.get("已占用吨位"))
            )
            if not eligibility.get("eligible"):
                return None, f"确认装载失败：{eligibility.get('reason')}"
            actual = cargo_tonnage(cargo)
            occupied_before = number(vessel.get("已占用吨位"))
            occupied_after = round(occupied_before + actual, 2)
            if occupied_after > dwt:
                return None, (
                    f"确认装载失败：装载后船舶占用 {occupied_after} 吨超过载重吨 {dwt} 吨"
                )

            rows = store.rows(self.MODULE)
            entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            batch_no = f"SINGLE-{cargo_ticket(cargo)}-{entry_id:04d}"
            cargo["status"] = LOADED
            cargo["pending"] = False
            cargo["abnormal"] = False
            cargo["装载船舶"] = text(vessel.get("船名")) or str(vessel.get("id"))
            cargo["装载批次号"] = batch_no
            cargo["配载回执编号"] = entry_id
            vessel["已占用吨位"] = occupied_after
            vessel["剩余载重吨"] = round(dwt - occupied_after, 2)
            entry: dict[str, Any] = {
                "id": entry_id,
                "status": CONFIRMED,
                "pending": False,
                "abnormal": False,
                "批次号": batch_no,
                "船舶档案": text(vessel.get("船名")) or str(vessel.get("id")),
                "船舶档案编号": vessel.get("id"),
                "舱位清单": text(cargo.get("舱位")) or "未分舱",
                "货票清单": cargo_ticket(cargo),
                "批次明细": [{
                    "cargo_id": int(cargo_id),
                    "货票编号": cargo_ticket(cargo),
                    "舱位": text(cargo.get("舱位")) or "未分舱",
                    "申报吨位": actual,
                    "result": LOADED,
                    "确认吨位": actual,
                    "说明": f"单票确认，装载后船舶占用 {occupied_after}/{dwt} 吨",
                }],
                "核准吨位": actual,
                "船舶载重吨": dwt,
                "确认前占用": occupied_before,
                "确认后占用": occupied_after,
                "剩余载重吨": round(dwt - occupied_after, 2),
                "备注说明": remark,
            }
            rows.append(entry)
            return cargo, (
                f"货物 {cargo_ticket(cargo)} 已确认装载，配载批次回执 {batch_no} 已关联船舶 "
                f"{entry['船舶档案']}，剩余载重吨 {entry['剩余载重吨']} 吨"
            )

    def release_cargo(self, cargo_id: int) -> tuple[dict[str, Any] | None, str]:
        with STOWAGE_LOCK:
            cargo = store.find("cargo", int(cargo_id))
            if cargo is None:
                return None, f"货物 {cargo_id} 不存在或已归档"
            if text(cargo.get("status")) != LOADED:
                return None, f"货物 {cargo_ticket(cargo)} 当前不是「已装载」，不能确认卸货"
            vessel = cargo_vessel(cargo)
            actual = cargo_tonnage(cargo)
            batch = None
            batch_id = cargo.get("配载回执编号")
            if batch_id not in (None, ""):
                batch = store.find(self.MODULE, int(batch_id))
            if vessel is not None and batch is not None:
                for detail in batch.get("批次明细") or []:
                    if int(detail.get("cargo_id", 0)) == int(cargo_id) and text(detail.get("result")) == LOADED:
                        detail["result"] = UNLOADED
                        detail["说明"] = "已确认卸货，吨位已从船舶占用中释放"
                        break
                occupied = max(round(number(vessel.get("已占用吨位")) - actual, 2), 0.0)
                vessel["已占用吨位"] = occupied
                vessel["剩余载重吨"] = round(vessel_dwt(vessel) - occupied, 2)
                batch["确认后占用"] = occupied
                batch["剩余载重吨"] = round(vessel_dwt(vessel) - occupied, 2)
            cargo["status"] = UNLOADED
            cargo["pending"] = False
            cargo["abnormal"] = False
            return cargo, (
                f"货物 {cargo_ticket(cargo)} 已确认卸货，释放 {actual} 吨；"
                f"船舶剩余载重吨 {number(vessel.get('剩余载重吨')) if vessel else 0} 吨"
            )

    # ---------------------------------------------------------------- 查询

    def eligibility(self, cargo_ref: Any, batch_id: int | None = None) -> dict[str, Any]:
        with STOWAGE_LOCK:
            cargo = resolve_cargo(cargo_ref)
            if cargo is None:
                return {"ok": False, "说明": f"货票 {cargo_ref} 不存在"}
            batch = self.get_entry(int(batch_id)) if batch_id is not None else None
            if batch is not None:
                vessel = resolve_vessel(batch.get("船舶档案") or batch.get("船舶档案编号"))
            else:
                vessel = cargo_vessel(cargo)
            if vessel is None:
                return {"ok": False, "说明": f"货票 {cargo_ticket(cargo)} 未关联船舶档案"}
            ensure_vessel_capacity(vessel)
            result = cargo_eligibility(
                cargo,
                vessel=vessel,
                occupied=number(vessel.get("已占用吨位")),
                exclude_batch_id=int(batch["id"]) if batch else None,
            )
            loaded_batch, _ = find_loaded_batch(
                int(cargo.get("id", 0)), exclude_batch_id=int(batch["id"]) if batch else None
            )
            return {
                "ok": True,
                "货票编号": cargo_ticket(cargo),
                "cargo_id": cargo.get("id"),
                "货票状态": text(cargo.get("status")),
                "申报吨位": cargo_tonnage(cargo),
                "舱位": text(cargo.get("舱位")),
                "船舶档案": text(vessel.get("船名")) or str(vessel.get("id")),
                "船舶档案编号": vessel.get("id"),
                "载重吨": vessel_dwt(vessel),
                "已占用吨位": number(vessel.get("已占用吨位")),
                "剩余载重吨": number(vessel.get("剩余载重吨")),
                "可单独确认": bool(result.get("eligible")),
                "需原批协调": bool(result.get("requires_coordination")),
                "余量不足": bool(result.get("capacity_short")),
                "结论": "可单独确认" if result.get("eligible") else (
                    CONFLICT if result.get("requires_coordination") else (
                        CAPACITY_SHORT if result.get("capacity_short") else INVALID
                    )
                ),
                "说明": result.get("reason"),
                "已在批次": loaded_batch.get("批次号") if loaded_batch else None,
                "批次状态": text(loaded_batch.get("status")) if loaded_batch else None,
            }

    def batches_for_cargo(self, cargo_ref: Any) -> list[dict[str, Any]]:
        cargo = resolve_cargo(cargo_ref)
        if cargo is None:
            return []
        result: list[dict[str, Any]] = []
        for batch in store.rows(self.MODULE):
            if any(
                int(detail.get("cargo_id", 0)) == int(cargo.get("id", 0))
                for detail in batch.get("批次明细") or []
            ):
                result.append(self.enrich(batch))
        return result

    def enrich(self, batch: dict[str, Any]) -> dict[str, Any]:
        """给回执补一份实时核对结果，供货代判断能否单独确认。"""
        copy = dict(batch)
        preview = self.preview(batch_id=int(batch.get("id", 0)))
        copy["当前核对"] = preview
        copy["可本批确认货票"] = preview.get("可本批确认货票", [])
        copy["本批已装载货票"] = preview.get("本批已装载货票", [])
        copy["单票可确认货票"] = preview.get("单票可确认货票", [])
        copy["需协调货票"] = preview.get("需协调货票", [])
        copy["余量不足货票"] = preview.get("余量不足货票", [])
        vessel = resolve_vessel(batch.get("船舶档案") or batch.get("船舶档案编号"))
        if vessel is not None:
            ensure_vessel_capacity(vessel)
            copy["船舶载重吨"] = vessel_dwt(vessel)
            copy["已占用吨位"] = number(vessel.get("已占用吨位"))
            copy["剩余载重吨"] = number(vessel.get("剩余载重吨"))
        return copy

    def list_batches(self) -> list[dict[str, Any]]:
        with STOWAGE_LOCK:
            return [self.enrich(row) for row in reversed(store.rows(self.MODULE))]

    # ---------------------------------------------------------------- 通用动作

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action in {"核对装载", "确认配载"}:
            return self.confirm_batch(entry_id)
        if action in {"退回协调", "退回原批", "退回"}:
            return self.return_batch(entry_id)
        return super().run_action(entry_id, action)

    def reweigh(self, cargo_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """补报称重：把货票实际吨位改为复磅值，已装载的按差额修正船舶占用。"""
        with STOWAGE_LOCK:
            cargo = store.find("cargo", int(cargo_id))
            if cargo is None:
                return None, f"货物 {cargo_id} 不存在或已归档"
            raw = (
                values.get("复磅吨数") or values.get("过磅吨数")
                or values.get("总吨数") or values.get("吨位") or values.get("吨数")
            )
            new_weight = number(raw)
            if new_weight <= 0:
                return None, "补报称重失败：复磅吨数必须大于 0"
            old_weight = cargo_tonnage(cargo)
            old_declared = cargo.get("原申报吨数")
            if old_declared in (None, ""):
                cargo["原申报吨数"] = old_weight
            cargo["总吨数"] = new_weight
            cargo["复磅吨数"] = new_weight
            if values.get("备注说明"):
                cargo["备注说明"] = text(values.get("备注说明"))

            batch_id = cargo.get("配载回执编号")
            batch = store.find(self.MODULE, int(batch_id)) if batch_id not in (None, "") else None
            vessel = cargo_vessel(cargo)
            if text(cargo.get("status")) == LOADED and batch is not None and vessel is not None:
                delta = round(new_weight - old_weight, 2)
                dwt = vessel_dwt(vessel)
                new_occupied = round(number(vessel.get("已占用吨位")) + delta, 2)
                if new_occupied > dwt:
                    return None, (
                        f"补报称重后船舶占用 {new_occupied} 吨将超过载重吨 {dwt} 吨，需先调整配载"
                    )
                if new_occupied < 0:
                    return None, "补报称重后船舶占用不能为负数"
                vessel["已占用吨位"] = new_occupied
                vessel["剩余载重吨"] = round(dwt - new_occupied, 2)
                for detail in batch.get("批次明细") or []:
                    if int(detail.get("cargo_id", 0)) == int(cargo_id):
                        detail["申报吨位"] = new_weight
                        detail["确认吨位"] = new_weight
                        break
                batch["核准吨位"] = round(number(batch.get("核准吨位")) + delta, 2)
                batch["确认后占用"] = new_occupied
                batch["剩余载重吨"] = round(dwt - new_occupied, 2)
            return cargo, (
                f"货票 {cargo_ticket(cargo)} 复磅吨数已更新为 {new_weight} 吨"
                + (f"，船舶剩余载重吨 {number(vessel.get('剩余载重吨'))} 吨" if vessel else "")
            )


stowage = StowageService()
