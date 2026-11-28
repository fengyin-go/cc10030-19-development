"""配载批次回执测试：批量核对、多选提交、货票唯一占用与船舶余量回写。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.stowage import stowage
from app.services.tonnage import number
from app.store import store

client = TestClient(app)


@pytest.fixture()
def isolated_vessel():
    """给每个用例一条独立船舶，避免和示例船的占用互相影响。"""
    vessel_id = 900
    while store.find("vessel", vessel_id) is not None:
        vessel_id += 1
    entry = {
        "id": vessel_id,
        "status": "已确认",
        "pending": False,
        "abnormal": False,
        "船名": f"S-{vessel_id}",
        "总吨位": "120",
        "载重吨": 100,
        "已占用吨位": 0,
        "剩余载重吨": 100,
        "建造年份": "2021",
        "船籍港": "上海港",
        "船长姓名": "测试船长",
        "当前状态": "待配载",
        "备注说明": "测试船舶",
    }
    store.rows("vessel").append(entry)
    return entry


def make_cargo(vessel: dict, ticket: str, tonnage: float, slot: str = "1舱") -> dict:
    rows = store.rows("cargo")
    entry = {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "status": "待装载",
        "pending": True,
        "abnormal": False,
        "货票编号": ticket,
        "货物品名": f"货物-{ticket}",
        "托运方": "测试托运方",
        "件数": "10",
        "总吨数": tonnage,
        "舱位": slot,
        "装载船舶": vessel["船名"],
        "装载航次": "",
        "装货港": "上海港",
        "卸货港": "宁波港",
        "备注说明": "",
    }
    rows.append(entry)
    return entry


def cargo_of(ticket: str) -> dict:
    for row in store.rows("cargo"):
        if row.get("货票编号") == ticket:
            return row
    raise AssertionError(f"没有找到货票 {ticket}")


def vessel_occupied(vessel: dict) -> float:
    return number(vessel.get("已占用吨位"))


class TestBatchPreview:
    def test_preview_requires_vessel_archive(self):
        result = stowage.preview(selections=["A"])
        assert result["ok"] is False
        assert "船舶档案" in result["说明"]

    def test_preview_uses_actual_reweighed_tonnage(self, isolated_vessel):
        cargo = make_cargo(isolated_vessel, "T-A", 20)
        cargo["复磅吨数"] = 35
        cargo["总吨数"] = 35
        result = stowage.preview(vessel_ref=isolated_vessel["船名"], selections=["T-A"])
        assert result["ok"] is True
        assert result["明细"][0]["申报吨位"] == 35
        assert result["确认后占用"] == 35

    def test_preview_multiselect_reports_over_capacity(self, isolated_vessel):
        make_cargo(isolated_vessel, "T-B", 30)
        make_cargo(isolated_vessel, "T-C", 40)
        make_cargo(isolated_vessel, "T-D", 40)
        result = stowage.preview(
            vessel_ref=isolated_vessel["船名"], selections=["T-B", "T-C", "T-D"]
        )
        assert result["ok"] is True
        assert result["确认后剩余"] > 0
        assert result["可本批确认货票"] == ["T-B", "T-C"]
        assert result["单票可确认货票"] == ["T-B", "T-C", "T-D"]
        assert result["需协调货票"] == []
        assert result["余量不足货票"] == ["T-D"]
        t_d = result["明细"][2]
        assert t_d["可确认"] is False
        assert t_d["单票可确认"] is True
        assert "拆批" in t_d["说明"]


class TestBatchSubmitAndConfirm:
    def test_submit_creates_receipt_linked_to_vessel(self, isolated_vessel):
        make_cargo(isolated_vessel, "T-E", 20, "1舱左")
        make_cargo(isolated_vessel, "T-F", 30, "2舱右")
        entry, message = stowage.submit_batch({
            "船舶档案": isolated_vessel["船名"],
            "货票清单": {"1舱左": ["T-E"], "2舱右": ["T-F"]},
        })
        assert entry is not None, message
        assert entry["status"] == "待核对"
        assert entry["船舶档案"] == isolated_vessel["船名"]
        assert entry["舱位清单"] == "1舱左、2舱右"
        assert entry["货票清单"] == "T-E、T-F"
        assert entry["船舶载重吨"] == 100

    def test_submit_rejects_unknown_vessel(self):
        entry, message = stowage.submit_batch({"船舶档案": "不存在船", "货票清单": ["A"]})
        assert entry is None
        assert "船舶档案" in message

    def test_confirm_batch_loads_multiple_tickets_and_updates_vessel(self, isolated_vessel):
        cargo_e = make_cargo(isolated_vessel, "T-G", 20)
        cargo_f = make_cargo(isolated_vessel, "T-H", 30)
        entry, _ = stowage.submit_batch({
            "船舶档案": isolated_vessel["船名"],
            "货票清单": ["T-G", "T-H"],
        })
        assert entry is not None
        confirmed, message = stowage.confirm_batch(int(entry["id"]))
        assert confirmed is not None, message
        assert confirmed["status"] == "已确认"
        assert confirmed["核准吨位"] == 50
        assert cargo_e["status"] == "已装载"
        assert cargo_f["status"] == "已装载"
        assert cargo_e["装载批次号"] == entry["批次号"]
        assert vessel_occupied(isolated_vessel) == 50
        assert isolated_vessel["剩余载重吨"] == 50

    def test_confirm_batch_never_exceeds_dwt(self, isolated_vessel):
        make_cargo(isolated_vessel, "T-I", 60)
        make_cargo(isolated_vessel, "T-J", 50)
        entry, _ = stowage.submit_batch({
            "船舶档案": isolated_vessel["船名"],
            "货票清单": ["T-I", "T-J"],
        })
        assert entry is not None
        confirmed, message = stowage.confirm_batch(int(entry["id"]))
        assert confirmed is not None, message
        assert confirmed["status"] == "部分确认"
        assert vessel_occupied(isolated_vessel) <= 100
        assert cargo_of("T-J")["status"] == "待装载"

    def test_returned_batch_keeps_unconfirmed_tickets_in_original_batch(self, isolated_vessel):
        make_cargo(isolated_vessel, "T-K", 40)
        entry, _ = stowage.submit_batch({
            "船舶档案": isolated_vessel["船名"],
            "货票清单": ["T-K"],
        })
        assert entry is not None
        returned, message = stowage.return_batch(int(entry["id"]), "等原批协调")
        assert returned is not None, message
        assert returned["status"] == "已退回"
        assert cargo_of("T-K")["status"] == "待装载"
        assert vessel_occupied(isolated_vessel) == 0
        # 退回后仍可在原批（同一回执）重新提交核对
        again, message = stowage.confirm_batch(int(entry["id"]))
        assert again is not None, message
        assert again["status"] == "已确认"


class TestCargoSingleConfirm:
    def test_single_confirm_creates_receipt_and_deducts_vessel(self, isolated_vessel):
        cargo = make_cargo(isolated_vessel, "T-L", 30)
        result, message = stowage.confirm_cargo(int(cargo["id"]))
        assert result is not None, message
        assert result["status"] == "已装载"
        assert result["装载批次号"].startswith("SINGLE-")
        assert vessel_occupied(isolated_vessel) == 30
        receipts = stowage.batches_for_cargo("T-L")
        assert len(receipts) == 1
        assert receipts[0]["status"] == "已确认"

    def test_single_confirm_rejects_over_capacity(self, isolated_vessel):
        make_cargo(isolated_vessel, "T-M", 80)
        make_cargo(isolated_vessel, "T-N", 40)
        stowage.confirm_cargo(int(cargo_of("T-M")["id"]))
        result, message = stowage.confirm_cargo(int(cargo_of("T-N")["id"]))
        assert result is None
        assert "剩余" in message or "超过" in message
        assert cargo_of("T-N")["status"] == "待装载"
        assert vessel_occupied(isolated_vessel) == 80

    def test_reweigh_loaded_ticket_adjusts_vessel_occupancy(self, isolated_vessel):
        cargo = make_cargo(isolated_vessel, "T-O", 20)
        stowage.confirm_cargo(int(cargo["id"]))
        assert vessel_occupied(isolated_vessel) == 20
        updated, message = stowage.reweigh(int(cargo["id"]), {"复磅吨数": 35})
        assert updated is not None, message
        assert updated["总吨数"] == 35
        assert updated["原申报吨数"] == 20
        assert vessel_occupied(isolated_vessel) == 35
        assert isolated_vessel["剩余载重吨"] == 65


class TestTicketUniqueness:
    def test_ticket_only_loaded_in_one_valid_batch(self, isolated_vessel):
        cargo = make_cargo(isolated_vessel, "T-P", 20)
        first, _ = stowage.submit_batch({
            "船舶档案": isolated_vessel["船名"], "货票清单": ["T-P"],
        })
        second, _ = stowage.submit_batch({
            "船舶档案": isolated_vessel["船名"], "货票清单": ["T-P"],
        })
        assert first is not None and second is not None
        ok_first, _ = stowage.confirm_batch(int(first["id"]))
        assert ok_first is not None
        ok_second, message = stowage.confirm_batch(int(second["id"]))
        assert ok_second is not None, message
        assert second["status"] == "已退回"
        assert second["批次明细"][0]["result"] == "待原批协调"
        assert vessel_occupied(isolated_vessel) == 20
        assert cargo["status"] == "已装载"

    def test_concurrent_batches_load_ticket_once(self, isolated_vessel):
        cargo = make_cargo(isolated_vessel, "T-Q", 35)
        make_cargo(isolated_vessel, "T-R", 30)
        make_cargo(isolated_vessel, "T-S", 40)
        first, _ = stowage.submit_batch({
            "船舶档案": isolated_vessel["船名"], "货票清单": ["T-Q", "T-R"],
        })
        second, _ = stowage.submit_batch({
            "船舶档案": isolated_vessel["船名"], "货票清单": ["T-Q", "T-S"],
        })
        assert first is not None and second is not None

        outcomes: dict[str, tuple] = {}
        barrier = threading.Barrier(2)

        def worker(name: str, batch_id: int) -> None:
            barrier.wait()
            outcomes[name] = stowage.confirm_batch(batch_id)

        threads = [
            threading.Thread(target=worker, args=("first", int(first["id"]))),
            threading.Thread(target=worker, args=("second", int(second["id"]))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert all(result is not None for result, _ in outcomes.values())
        assert cargo["status"] == "已装载"
        assert vessel_occupied(isolated_vessel) <= 100
        loaded_receipts = [
            receipt for receipt in (first, second)
            if receipt["status"] in {"已确认", "部分确认"}
            and any(
                detail["货票编号"] == "T-Q" and detail["result"] == "已装载"
                for detail in receipt["批次明细"]
            )
        ]
        assert len(loaded_receipts) == 1

    def test_capacity_short_ticket_stays_pending_and_can_retry(self, isolated_vessel):
        make_cargo(isolated_vessel, "T-T", 65)
        make_cargo(isolated_vessel, "T-U", 40)
        first, _ = stowage.submit_batch({
            "船舶档案": isolated_vessel["船名"], "货票清单": ["T-T"],
        })
        second, _ = stowage.submit_batch({
            "船舶档案": isolated_vessel["船名"], "货票清单": ["T-U"],
        })
        assert first is not None and second is not None
        stowage.confirm_batch(int(first["id"]))
        confirmed, message = stowage.confirm_batch(int(second["id"]))
        assert confirmed is not None, message
        assert cargo_of("T-U")["status"] == "待装载"
        assert vessel_occupied(isolated_vessel) == 65
        status = stowage.eligibility("T-U")
        assert status["可单独确认"] is False
        assert status["需原批协调"] is True
        assert status["余量不足"] is True
        assert "原批释放吨位" in status["说明"]
        # 原批释放吨位后，同一回执可以重新核对
        stowage.release_cargo(int(cargo_of("T-T")["id"]))
        retry, message = stowage.confirm_batch(int(second["id"]))
        assert retry is not None, message
        assert cargo_of("T-U")["status"] == "已装载"
        assert vessel_occupied(isolated_vessel) == 40


class TestHttpApi:
    def test_preview_api(self):
        result = client.post(
            "/api/stowage/preview", json={"船舶档案": "S6", "货票": ["A", "B"]}
        ).json()
        assert result["ok"] is True
        assert result["载重吨"] == 100
        assert [item["货票编号"] for item in result["明细"]] == ["A", "B"]

    def test_cargo_status_api_reports_solo_or_coordination(self):
        result = client.get("/api/stowage/cargo/A").json()
        assert result["ok"] is True
        assert result["结论"] in {"可单独确认", "待原批协调", "不可装载"}

    def test_vessel_capacity_api(self):
        result = client.get("/api/stowage/vessels/S6").json()
        assert result["ok"] is True
        assert result["载重吨"] == 100

    def test_generic_batch_module_lists_receipts(self):
        result = client.get("/api/stowage_batch").json()
        assert result["total"] >= 2
        row = result["items"][0]
        assert row["船舶档案"] == "S6"
        assert row["id"] is not None
