"""`scripts/regime_call.py` — ghi nhận định regime "shadow" của nhóm AI (research/regime_shadow.md). Viết TRƯỚC khi
implement.

Luật:
- Chỉ ghi được trong NGÀY GIAO DỊCH và TRƯỚC `session.ato_start` (sau đó giá đã lộ ⇒ nhận định không còn là dự báo).
- Giờ ghi (`created_at`), ngày (`trade_date`) và hợp đồng do SCRIPT đóng dấu — payload của AI không được tự khai.
- Mỗi ngày đúng MỘT file, không ghi đè (không sửa nhận định sau khi biết kết quả).
- Ba câu hỏi, mỗi câu một phân phối xác suất trên đúng các lớp đã đăng ký; có cả `debate` (sau tranh luận) và `single`
  (một agent, làm TRƯỚC tranh luận) để đo xem tranh luận có thêm giá trị không.
- Giờ lấy từ đồng hồ hệ thống; dòng lệnh KHÔNG có cờ đặt giờ (test tiêm `now` qua tham số hàm).
- `--dry-run` (thử đường ống): bỏ chốt giờ, ghi vào thư mục con `dryrun/`, không bao giờ được chấm điểm.
"""
import json
from datetime import date, datetime

import pytest

from core.clock import SessionSchedule
from scripts.regime_call import (QUESTIONS, AlreadyRecorded, CallError, NotTradingDay, WindowClosed, call_window,
                                 load_calls, main, validate_forecast, write_call)

MON = date(2026, 11, 16)          # thứ Hai
SAT = date(2026, 11, 21)
HOLIDAY = date(2026, 9, 2)        # có trong fixture `cal`


def at(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm)


def forecast(**over) -> dict:
    f = {"vol": {"low": 0.2, "normal": 0.5, "high": 0.3},
         "day_type": {"trend": 0.4, "range": 0.6},
         "direction": {"up": 0.3, "flat": 0.4, "down": 0.3}}
    f.update(over)
    return f


def payload(**over) -> dict:
    p = {"model": "model-x",
         "inputs": {"sources": ["DNSE price_get_history"], "adr10": 31.5, "prev_close": 1890.0},
         "single": forecast(),
         "debate": forecast(day_type={"trend": 0.55, "range": 0.45}),
         "transcript": {"single": "…", "trend_advocate": "…", "reversion_advocate": "…", "skeptic": "…",
                        "judge": "…"}}
    p.update(over)
    return p


@pytest.fixture
def schedule(session_cfg) -> SessionSchedule:
    return SessionSchedule.from_mapping(session_cfg)


# ------------------------------------------------------------------ dự báo ----

def test_questions_are_the_registered_ones():
    assert QUESTIONS == {"vol": ("low", "normal", "high"), "day_type": ("trend", "range"),
                         "direction": ("up", "flat", "down")}


def test_validate_forecast_accepts_and_returns_floats():
    out = validate_forecast(forecast())
    assert out["vol"] == {"low": 0.2, "normal": 0.5, "high": 0.3}
    assert all(isinstance(v, float) for q in out.values() for v in q.values())


@pytest.mark.parametrize("bad", [
    {"vol": {"low": 0.2, "normal": 0.5, "high": 0.4}},                 # tổng 1,1
    {"vol": {"low": 0.5, "normal": 0.5}},                              # thiếu lớp
    {"vol": {"low": 0.2, "normal": 0.5, "high": 0.3, "extreme": 0.0}},  # thừa lớp
    {"vol": {"low": -0.1, "normal": 0.6, "high": 0.5}},                # âm
    {"vol": {"low": "nhiều", "normal": 0.5, "high": 0.5}},             # không phải số
    {"vol": {"low": True, "normal": 0.0, "high": 0.0}},                # bool không phải xác suất
    {"day_type": "trend"},                                             # nhãn cứng, không phải phân phối
])
def test_validate_forecast_rejects(bad):
    with pytest.raises(CallError):
        validate_forecast(forecast(**bad))


def test_validate_forecast_rejects_missing_or_extra_question():
    f = forecast()
    del f["direction"]
    with pytest.raises(CallError):
        validate_forecast(f)
    with pytest.raises(CallError):
        validate_forecast(forecast(gap={"up": 1.0}))


# ------------------------------------------------------------------ cửa sổ giờ ----

def test_window_open_before_ato(schedule, cal):
    assert call_window(at(MON, 7, 55), schedule, cal) == MON
    assert call_window(at(MON, 8, 44), schedule, cal) == MON          # pre-open 08:30 vẫn ghi được


def test_window_closed_from_ato(schedule, cal):
    for hh, mm in ((8, 45), (9, 0), (11, 45), (15, 0), (23, 30)):
        with pytest.raises(WindowClosed):
            call_window(at(MON, hh, mm), schedule, cal)


def test_window_rejects_non_trading_day(schedule, cal):
    for d in (SAT, HOLIDAY):
        with pytest.raises(NotTradingDay):
            call_window(at(d, 7, 55), schedule, cal)


# ------------------------------------------------------------------ ghi ----

def test_write_call_stamps_time_date_contract(tmp_path, schedule, cal):
    p = write_call(payload(), tmp_path, now=at(MON, 8, 3), schedule=schedule, calendar=cal)
    assert p == tmp_path / "2026-11-16.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    assert rec["schema_version"] == 1 and rec["trade_date"] == "2026-11-16" and rec["dry_run"] is False
    assert rec["created_at"] == "2026-11-16T08:03:00+07:00"
    assert rec["contract"] == "VN30F2611"                              # 16/11 chưa tới ngày đáo hạn 19/11
    assert rec["debate"]["day_type"] == {"trend": 0.55, "range": 0.45}
    assert rec["single"]["day_type"] == {"trend": 0.4, "range": 0.6}
    assert rec["model"] == "model-x" and rec["inputs"]["adr10"] == 31.5
    assert set(rec["transcript"]) == {"single", "trend_advocate", "reversion_advocate", "skeptic", "judge"}


def test_write_call_uses_rolled_contract_on_expiry_day(tmp_path, schedule, cal):
    p = write_call(payload(), tmp_path, now=at(date(2026, 11, 19), 8, 0), schedule=schedule, calendar=cal,
                   roll_days=1)
    assert json.loads(p.read_text(encoding="utf-8"))["contract"] == "VN30F2612"


def test_write_call_refuses_after_open_and_on_holiday(tmp_path, schedule, cal):
    with pytest.raises(WindowClosed):
        write_call(payload(), tmp_path, now=at(MON, 8, 45), schedule=schedule, calendar=cal)
    with pytest.raises(NotTradingDay):
        write_call(payload(), tmp_path, now=at(SAT, 8, 0), schedule=schedule, calendar=cal)
    assert list(tmp_path.iterdir()) == []


def test_write_call_never_overwrites(tmp_path, schedule, cal):
    write_call(payload(), tmp_path, now=at(MON, 8, 3), schedule=schedule, calendar=cal)
    before = (tmp_path / "2026-11-16.json").read_text(encoding="utf-8")
    with pytest.raises(AlreadyRecorded):
        write_call(payload(model="model-y"), tmp_path, now=at(MON, 8, 20), schedule=schedule, calendar=cal)
    assert (tmp_path / "2026-11-16.json").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("bad", [
    {"model": ""},
    {"inputs": {}},
    {"inputs": "đã xem tin"},
    {"transcript": {"judge": "…"}},                                                    # thiếu vai
    {"transcript": {"single": "…", "trend_advocate": "", "reversion_advocate": "…", "skeptic": "…", "judge": "…"}},
    {"created_at": "2026-11-16T07:00:00+07:00"},                                       # AI tự khai giờ
    {"trade_date": "2026-11-17"},
    {"contract": "VN30F2612"},
    {"note": "thêm trường lạ"},
])
def test_write_call_rejects_bad_payload(tmp_path, schedule, cal, bad):
    with pytest.raises(CallError):
        write_call(payload(**bad), tmp_path, now=at(MON, 8, 3), schedule=schedule, calendar=cal)
    assert list(tmp_path.iterdir()) == []


def test_write_call_rejects_missing_forecaster(tmp_path, schedule, cal):
    p = payload()
    del p["single"]
    with pytest.raises(CallError):
        write_call(p, tmp_path, now=at(MON, 8, 3), schedule=schedule, calendar=cal)


def test_dry_run_skips_time_gate_and_goes_to_subdir(tmp_path, schedule, cal):
    p = write_call(payload(), tmp_path, now=at(MON, 10, 40), schedule=schedule, calendar=cal, dry_run=True)
    assert p == tmp_path / "dryrun" / "2026-11-16T104000.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    assert rec["dry_run"] is True and rec["trade_date"] == "2026-11-16"
    assert not (tmp_path / "2026-11-16.json").exists()
    # ngày nghỉ vẫn thử được đường ống
    assert write_call(payload(), tmp_path, now=at(SAT, 10, 0), schedule=schedule, calendar=cal,
                      dry_run=True).parent.name == "dryrun"


# ------------------------------------------------------------------ đọc ----

def test_load_calls_sorted_and_ignores_dry_runs(tmp_path, schedule, cal):
    write_call(payload(), tmp_path, now=at(date(2026, 11, 17), 8, 1), schedule=schedule, calendar=cal)
    write_call(payload(), tmp_path, now=at(MON, 8, 3), schedule=schedule, calendar=cal)
    write_call(payload(), tmp_path, now=at(MON, 10, 0), schedule=schedule, calendar=cal, dry_run=True)
    calls = load_calls(tmp_path, schedule)
    assert [c["trade_date"] for c in calls] == ["2026-11-16", "2026-11-17"]


def test_load_calls_empty_or_missing_dir(tmp_path, schedule):
    assert load_calls(tmp_path / "nope", schedule) == []


def _tamper(path, **over):
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec.update(over)
    path.write_text(json.dumps(rec), encoding="utf-8")


@pytest.mark.parametrize("over", [
    {"created_at": "2026-11-16T09:10:00+07:00"},        # sửa tay thành giờ sau mở cửa
    {"created_at": "2026-11-15T20:00:00+07:00"},        # không phải trong ngày giao dịch đó
    {"trade_date": "2026-11-18"},                       # không khớp tên file
    {"dry_run": True},
    {"schema_version": 99},
    {"debate": {"vol": {"low": 1.0}}},
])
def test_load_calls_rejects_tampered_file(tmp_path, schedule, cal, over):
    p = write_call(payload(), tmp_path, now=at(MON, 8, 3), schedule=schedule, calendar=cal)
    _tamper(p, **over)
    with pytest.raises(CallError):
        load_calls(tmp_path, schedule)


# ------------------------------------------------------------------ CLI ----

def test_cli_check_exit_codes(tmp_path, capsys):
    assert main(["--check", "--out", str(tmp_path)], now=at(MON, 8, 0)) == 0
    assert "2026-11-16" in capsys.readouterr().out
    assert main(["--check", "--out", str(tmp_path)], now=at(SAT, 8, 0)) == 3      # thứ Bảy
    assert main(["--check", "--out", str(tmp_path)], now=at(MON, 8, 45)) == 4      # đã ATO
    write_payload = tmp_path / "p.json"
    write_payload.write_text(json.dumps(payload()), encoding="utf-8")
    assert main(["--payload", str(write_payload), "--out", str(tmp_path / "calls")], now=at(MON, 8, 0)) == 0
    assert main(["--check", "--out", str(tmp_path / "calls")], now=at(MON, 8, 10)) == 5   # đã ghi rồi


def test_cli_write_and_errors(tmp_path, capsys):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(payload()), encoding="utf-8")
    bad.write_text(json.dumps(payload(model="")), encoding="utf-8")
    out = tmp_path / "calls"
    assert main(["--payload", str(bad), "--out", str(out)], now=at(MON, 8, 0)) == 2
    assert main(["--payload", str(good), "--out", str(out)], now=at(MON, 8, 50)) == 4
    assert main(["--payload", str(good), "--out", str(out)], now=at(MON, 8, 0)) == 0
    assert (out / "2026-11-16.json").exists()
    assert main(["--payload", str(good), "--out", str(out)], now=at(MON, 8, 5)) == 5
    assert main(["--payload", str(good), "--out", str(out), "--dry-run"], now=at(MON, 10, 30)) == 0
    assert len(list((out / "dryrun").glob("*.json"))) == 1
    assert main(["--payload", str(tmp_path / "missing.json"), "--out", str(out)], now=at(MON, 8, 0)) == 2
