"""Adapter DNSE đọc đúng response THẬT (tests/fixtures/dnse_real_20261109.json, probe 09/11/2026).

Phát hiện: `/accounts/{tk}/positions` trả danh sách trong khoá `positions` (kèm pageIndex/pageSize/pageNumber/
total) — KHÔNG phải `deals` như SDK ⇒ adapter cũ sẽ báo lỗi mỗi lần đọc vị thế (runner từ chối khởi động).
"""
import json
from pathlib import Path

import pytest

from brokers.base import BrokerError
from brokers.dnse import _order_list
from core import contracts
from tests.test_broker_dnse import KRX, SYM, make, run

REAL = json.loads((Path(__file__).parent / "fixtures" / "dnse_real_20261109.json").read_text(encoding="utf-8"))


def test_real_empty_orders_body_parsed():
    assert _order_list(REAL["orders"]) == []


def test_real_empty_positions_body_parsed():
    b, tr, *_ = make()
    tr.positions_body = REAL["positions"]
    assert run(b.positions()) == {}


def test_positions_key_with_items():
    b, tr, *_ = make()
    tr.positions_body = {"positions": [
        {"id": 1, "symbol": KRX, "side": "BUY", "status": "OPEN", "openQuantity": 2, "costPrice": 1350.0},
        {"id": 2, "symbol": KRX, "side": "NB", "status": "OPEN", "openQuantity": 1, "costPrice": 1353.0}],
        "pageIndex": 0, "pageSize": 100, "pageNumber": 0, "total": 2}
    p = run(b.positions())[SYM]
    assert p.qty == 3 and p.avg_price == pytest.approx(1351.0)


def test_legacy_deals_key_still_accepted():
    b, tr, *_ = make()
    tr.positions_body = {"deals": [{"id": 1, "symbol": KRX, "side": "SELL", "status": "OPEN", "openQuantity": 1,
                                    "costPrice": 1350.0}]}
    assert run(b.positions())[SYM].qty == -1


def test_unknown_positions_shape_refused():
    b, tr, *_ = make()
    tr.positions_body = {"items": []}
    with pytest.raises(BrokerError, match="positions"):
        run(b.positions())


def test_total_larger_than_page_refused_with_real_shape():
    b, tr, *_ = make()
    tr.positions_body = {"positions": [], "pageIndex": 0, "pageSize": 100, "pageNumber": 0, "total": 3}
    with pytest.raises(BrokerError, match="thiếu trang"):
        run(b.positions())


def test_secdef_confirms_price_band_and_krx():
    s = REAL["secdef"][0]
    assert contracts.symbol_contract(s["symbol"]) == "VN30F2611"
    assert s["ceilingPrice"] == pytest.approx(round(s["basicPrice"] * 1.07, 1), abs=0.1)   # ±7%, đơn vị điểm
    assert s["floorPrice"] == pytest.approx(round(s["basicPrice"] * 0.93, 1), abs=0.1)
