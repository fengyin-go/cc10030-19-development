"""吨位与货票解析辅助：配载回执、货物登记和船舶档案共用同一套口径。

这一层只做读取和口径统一，所有写操作（货票装载、船舶占用增减）由
`stowage` 服务在配载锁内完成，避免并发确认时重复扣减。
"""
from __future__ import annotations

from typing import Any

from app.store import store

LOADED = "已装载"
UNLOADED = "已卸货"
RETURNED = "已退回"
PARTIAL = "部分确认"
CONFIRMED = "已确认"
WAITING = "待核对"
VALID_BATCH_STATUSES = {CONFIRMED, PARTIAL}


def number(value: Any) -> float:
    """把界面、回执里的吨位字段安全地转成两位小数。"""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    raw = str(value if value is not None else "").replace(",", "").strip()
    if not raw:
        return 0.0
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        return 0.0


def text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def resolve_vessel(identifier: Any) -> dict[str, Any] | None:
    """船舶编号既可以是档案 id，也可以是船名（例如 S6）。"""
    raw = text(identifier)
    if not raw:
        return None
    if raw.isdigit():
        row = store.find("vessel", int(raw))
        if row is not None:
            return row
    for row in store.rows("vessel"):
        if text(row.get("船名")) == raw or text(row.get("id")) == raw:
            return row
    return None


def resolve_cargo(identifier: Any) -> dict[str, Any] | None:
    """货票既可以用登记 id，也可以用票号（例如 A、B、D）。"""
    if isinstance(identifier, dict):
        identifier = identifier.get("id") or identifier.get("货票编号")
    raw = text(identifier)
    if not raw:
        return None
    if raw.isdigit():
        row = store.find("cargo", int(raw))
        if row is not None:
            return row
    for row in store.rows("cargo"):
        if text(row.get("货票编号")) == raw or text(row.get("id")) == raw:
            return row
    return None


def cargo_ticket(cargo: dict[str, Any]) -> str:
    return text(cargo.get("货票编号")) or f"#{cargo.get('id')}"


def cargo_tonnage(cargo: dict[str, Any]) -> float:
    return number(cargo.get("总吨数"))


def vessel_dwt(vessel: dict[str, Any]) -> float:
    return number(vessel.get("载重吨"))


def ensure_vessel_capacity(vessel: dict[str, Any]) -> dict[str, Any]:
    """补齐船舶档案的占用/余量字段，返回同一个行对象。"""
    dwt = vessel_dwt(vessel)
    occupied = number(vessel.get("已占用吨位", 0))
    if occupied < 0:
        occupied = 0.0
    if occupied > dwt and dwt > 0:
        # 历史数据若已经超载，保留真实占用值便于暴露异常，不回写成负数。
        occupied = round(occupied, 2)
    vessel["已占用吨位"] = round(occupied, 2)
    vessel["剩余载重吨"] = round(max(dwt - occupied, 0.0), 2)
    return vessel


def vessel_capacity(vessel: dict[str, Any]) -> dict[str, Any]:
    ensure_vessel_capacity(vessel)
    return {
        "船舶档案编号": vessel.get("id"),
        "船名": text(vessel.get("船名")),
        "载重吨": vessel_dwt(vessel),
        "已占用吨位": number(vessel.get("已占用吨位")),
        "剩余载重吨": number(vessel.get("剩余载重吨")),
    }


def find_loaded_batch(cargo_id: int, exclude_batch_id: int | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """查找已经把该货票实际装载进船的有效配载批次。"""
    for batch in store.rows("stowage_batch"):
        if text(batch.get("status")) not in VALID_BATCH_STATUSES:
            continue
        if exclude_batch_id is not None and int(batch.get("id", 0)) == int(exclude_batch_id):
            continue
        for item in batch.get("批次明细") or []:
            if text(item.get("result")) == LOADED and int(item.get("cargo_id", 0)) == int(cargo_id):
                return batch, item
    return None, None


def voyage_vessel(voyage_id: Any) -> dict[str, Any] | None:
    voyage = store.find("voyage", int(voyage_id)) if text(voyage_id).isdigit() else None
    if voyage is None:
        for row in store.rows("voyage"):
            if text(row.get("id")) == text(voyage_id):
                voyage = row
                break
    if voyage is None:
        return None
    return resolve_vessel(voyage.get("船舶编号"))


def cargo_vessel(cargo: dict[str, Any]) -> dict[str, Any] | None:
    vessel = resolve_vessel(cargo.get("装载船舶")) if cargo.get("装载船舶") else None
    if vessel is not None:
        return vessel
    return voyage_vessel(cargo.get("装载航次"))


def cargo_eligibility(
    cargo: dict[str, Any],
    *,
    vessel: dict[str, Any] | None = None,
    occupied: float | None = None,
    exclude_batch_id: int | None = None,
) -> dict[str, Any]:
    """计算单张货票此刻能否被单独/批次确认。"""
    ticket = cargo_ticket(cargo)
    actual = cargo_tonnage(cargo)
    status = text(cargo.get("status"))
    target_vessel = vessel or cargo_vessel(cargo)

    if status in {UNLOADED, RETURNED}:
        return {
            "eligible": False, "ticket": ticket, "cargo_id": cargo.get("id"),
            "tonnage": actual, "reason": f"货票 {ticket} 状态为「{status}」，不能再次装载",
            "requires_coordination": False,
        }
    if status == LOADED:
        batch, item = find_loaded_batch(int(cargo.get("id", 0)), exclude_batch_id)
        if batch is not None:
            reason = f"货票 {ticket} 已在配载批次 {batch.get('批次号')} 实际装载，需与原批协调"
        elif item is not None:
            reason = f"货票 {ticket} 已在本批次实际装载"
        else:
            reason = f"货票 {ticket} 已处于「已装载」，不能重复确认"
        return {
            "eligible": False, "ticket": ticket, "cargo_id": cargo.get("id"),
            "tonnage": actual, "reason": reason, "requires_coordination": batch is not None,
            "loaded_batch": batch.get("批次号") if batch else None,
        }
    if status not in {"草稿", "待装载", ""}:
        return {
            "eligible": False, "ticket": ticket, "cargo_id": cargo.get("id"),
            "tonnage": actual, "reason": f"货票 {ticket} 状态为「{status}」，需先回到待装载",
            "requires_coordination": False,
        }
    if actual <= 0:
        return {
            "eligible": False, "ticket": ticket, "cargo_id": cargo.get("id"),
            "tonnage": actual, "reason": f"货票 {ticket} 申报吨数必须大于 0",
            "requires_coordination": False,
        }
    if target_vessel is None:
        return {
            "eligible": False, "ticket": ticket, "cargo_id": cargo.get("id"),
            "tonnage": actual, "reason": f"货票 {ticket} 未关联有效船舶档案",
            "requires_coordination": False,
        }

    ensure_vessel_capacity(target_vessel)
    occupied_now = number(target_vessel.get("已占用吨位")) if occupied is None else occupied
    dwt = vessel_dwt(target_vessel)
    remaining = round(dwt - occupied_now, 2)
    if actual > remaining:
        return {
            "eligible": False, "ticket": ticket, "cargo_id": cargo.get("id"),
            "tonnage": actual, "reason": (
                f"货票 {ticket} 需 {actual} 吨，船舶剩余载重吨仅 {remaining} 吨；"
                "这张票要等原批释放吨位或调整装载后才能确认"
            ),
            "requires_coordination": True,
            "capacity_short": True,
            "remaining": remaining,
        }
    return {
        "eligible": True, "ticket": ticket, "cargo_id": cargo.get("id"),
        "tonnage": actual, "reason": (
            f"货票 {ticket} 可单独确认，装载后船舶占用 {round(occupied_now + actual, 2)}/{dwt} 吨"
        ),
        "requires_coordination": False,
        "remaining": remaining,
        "vessel_id": target_vessel.get("id"),
        "vessel_name": text(target_vessel.get("船名")),
    }
