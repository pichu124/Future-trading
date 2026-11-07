from datetime import date

import pytest

from core import contracts as c


class TestNames:
    def test_contract_name_roundtrip(self):
        assert c.contract_name(2026, 3) == "VN30F2603"
        assert c.contract_ym("VN30F2612") == (2026, 12)

    def test_contract_name_rejects_bad_month(self):
        with pytest.raises(ValueError):
            c.contract_name(2026, 13)

    def test_krx_symbol_matches_measured_letters(self):
        # đã đo ở SSI (vnstock-dashboard 15/10/2026): 2024=E … 2027=H
        assert c.krx_symbol(2024, 1) == "41I1E1000"
        assert c.krx_symbol(2026, 4) == "41I1G4000"
        assert c.krx_symbol(2026, 10) == "41I1GA000"
        assert c.krx_symbol(2027, 12) == "41I1HC000"

    def test_codes_confirmed_by_external_sources(self):
        # MBS (quy định giao dịch HĐTL VN30): 41I1F6000 = VN30 tháng 06/2025
        assert c.symbol_contract("41I1F6000") == "VN30F2506"
        # Vietstock: 41I1G9000 = VN30 tháng 09/2026, ngày GDCC 17/09/2026
        assert c.symbol_contract("41I1G9000") == "VN30F2609"
        # Ví dụ đặt lệnh trong đặc tả DNSE Open API V2
        assert c.symbol_contract("41I1G4000") == "VN30F2604"

    def test_last_trading_day_matches_vietstock(self, cal):
        assert c.contract_expiry("VN30F2609", cal) == date(2026, 9, 17)

    def test_symbol_contract_accepts_all_fixed_forms(self):
        assert c.symbol_contract("41I1G4000") == "VN30F2604"
        assert c.symbol_contract(" vn30f2611 ") == "VN30F2611"
        assert c.symbol_contract("41I1GB000") == "VN30F2611"
        assert c.symbol_contract("VN30F1M") is None   # cuốn chiếu — cần ngày
        assert c.symbol_contract("FPT") is None
        assert c.symbol_contract(None) is None

    def test_krx_roundtrip_every_month(self):
        for y in range(2024, 2028):
            for m in range(1, 13):
                assert c.symbol_contract(c.krx_symbol(y, m)) == c.contract_name(y, m)

    def test_unverified_year_flag(self):
        assert c.krx_year_verified(2027)
        assert not c.krx_year_verified(2028)

    def test_is_vn30_future(self):
        for s in ("VN30F1M", "VN30F2Q", "VN30F2611", "41I1GB000"):
            assert c.is_vn30_future(s)
        for s in ("VN30", "41I2GB000", "FPT", ""):
            assert not c.is_vn30_future(s)


class TestExpiry:
    @pytest.mark.parametrize("m,expected_day", [
        (1, 15), (3, 19), (4, 16), (5, 21), (6, 18), (7, 16),
        (8, 20), (9, 17), (10, 15), (11, 19), (12, 17),
    ])
    def test_third_thursday_2026(self, m, expected_day):
        d = c.third_thursday(2026, m)
        assert d == date(2026, m, expected_day)
        assert d.weekday() == 3

    def test_expiry_shifts_before_tet(self, cal):
        # VN30F2602: thứ Năm tuần 3 là 19/02 (Tết) ⇒ đáo hạn 13/02 (thứ Sáu trước)
        assert c.third_thursday(2026, 2) == date(2026, 2, 19)
        assert c.expiry_date(2026, 2, cal) == date(2026, 2, 13)
        assert c.contract_expiry("VN30F2602", cal) == date(2026, 2, 13)

    def test_expiry_unchanged_on_normal_month(self, cal):
        assert c.expiry_date(2026, 11, cal) == date(2026, 11, 19)

    def test_expiry_2027_is_third_thursday_when_no_holiday(self, cal):
        for m in range(1, 13):
            d = c.expiry_date(2027, m, cal)
            assert d.weekday() == 3 and 15 <= d.day <= 21


class TestFrontMonthAndRoll:
    def test_front_month_until_expiry_inclusive(self, cal):
        assert c.front_month(date(2026, 11, 7), cal) == "VN30F2611"
        assert c.front_month(date(2026, 11, 19), cal) == "VN30F2611"   # ngày đáo hạn vẫn là F1M
        assert c.front_month(date(2026, 11, 20), cal) == "VN30F2612"

    def test_front_month_after_shifted_tet_expiry(self, cal):
        assert c.front_month(date(2026, 2, 13), cal) == "VN30F2602"
        assert c.front_month(date(2026, 2, 23), cal) == "VN30F2603"

    def test_listed_contracts(self, cal):
        listed = c.listed_contracts(date(2026, 11, 7), cal)
        assert listed == {"VN30F1M": "VN30F2611", "VN30F2M": "VN30F2612",
                          "VN30F1Q": "VN30F2703", "VN30F2Q": "VN30F2706"}

    def test_listed_contracts_skips_quarter_already_in_months(self, cal):
        # F1M=12/2026, F2M=01/2027 ⇒ quý: 03/2027, 06/2027
        listed = c.listed_contracts(date(2026, 12, 1), cal)
        assert listed["VN30F2M"] == "VN30F2701"
        assert listed["VN30F1Q"] == "VN30F2703"
        assert listed["VN30F2Q"] == "VN30F2706"

    def test_resolve_symbol_handles_every_form(self, cal):
        d = date(2026, 11, 7)
        assert c.resolve_symbol("VN30F1M", d, cal) == "VN30F2611"
        assert c.resolve_symbol("VN30F2M", d, cal) == "VN30F2612"
        assert c.resolve_symbol("41I1GB000", d, cal) == "VN30F2611"
        assert c.resolve_symbol("VCB", d, cal) is None

    def test_roll_one_day_means_trade_f2m_on_expiry_day(self, cal):
        assert c.trading_contract(date(2026, 11, 18), cal, 1) == "VN30F2611"
        assert c.trading_contract(date(2026, 11, 19), cal, 1) == "VN30F2612"
        assert c.roll_date("VN30F2611", cal, 1) == date(2026, 11, 19)

    def test_roll_two_days(self, cal):
        assert c.trading_contract(date(2026, 11, 17), cal, 2) == "VN30F2611"
        assert c.trading_contract(date(2026, 11, 18), cal, 2) == "VN30F2612"
        assert c.roll_date("VN30F2611", cal, 2) == date(2026, 11, 18)

    def test_roll_zero_keeps_f1m_through_expiry(self, cal):
        assert c.trading_contract(date(2026, 11, 19), cal, 0) == "VN30F2611"
        assert c.roll_date("VN30F2611", cal, 0) == date(2026, 11, 20)

    def test_roll_window_crosses_holiday(self, cal):
        # VN30F2602 đáo hạn 13/02 (thứ Sáu); roll 2 phiên ⇒ từ 12/02
        assert c.roll_date("VN30F2602", cal, 2) == date(2026, 2, 12)
        assert c.trading_contract(date(2026, 2, 12), cal, 2) == "VN30F2603"

    def test_trading_contract_and_roll_date_agree(self, cal):
        """Với mọi ngày giao dịch: hợp đồng giao dịch hôm nay ≠ hợp đồng có roll_date ≤ hôm nay."""
        d = date(2026, 1, 2)
        while d < date(2027, 12, 31):
            if cal.is_trading_day(d):
                for n in (0, 1, 2, 3):
                    current = c.trading_contract(d, cal, n)
                    assert c.roll_date(current, cal, n) > d, (d, n, current)
            d = date.fromordinal(d.toordinal() + 1)

    def test_is_expiry_day(self, cal):
        assert c.is_expiry_day(date(2026, 11, 19), cal)
        assert not c.is_expiry_day(date(2026, 11, 18), cal)
        assert c.is_expiry_day(date(2026, 2, 13), cal)

    def test_next_contract_roll_date(self, cal):
        assert c.next_contract_roll_date(date(2026, 11, 7), cal, 1) == date(2026, 11, 19)
        # đã ở trong cửa sổ roll ⇒ ngày roll kế là của F2M
        assert c.next_contract_roll_date(date(2026, 11, 19), cal, 1) == date(2026, 12, 17)

    def test_negative_roll_rejected(self, cal):
        with pytest.raises(ValueError):
            c.trading_contract(date(2026, 11, 7), cal, -1)
