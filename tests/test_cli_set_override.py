"""`--set khoá.chấm=giá_trị` cho `backtest.engine` và `backtest.robustness` — viết TRƯỚC.

Đổi cấu hình CHỈ cho lần chạy đó (vd bật ORB #2 `strategy.orb.range_filter.enabled=true`) mà không sửa
`config/settings.yaml` (bật trong settings thì runner live/paper cũng đổi theo). Khoá phải tồn tại (chống gõ sai),
giá trị đọc như YAML (true/false/số/chuỗi).
"""
import pytest

from backtest.robustness import parse_sets


def test_parse_sets_yaml_values():
    assert parse_sets(["strategy.orb.range_filter.enabled=true", "risk.max_trades_per_day=3",
                       "strategy.orb.reward_risk=2.5", "live.allowed_machine=DESKTOP-X"]) == {
        "strategy.orb.range_filter.enabled": True, "risk.max_trades_per_day": 3,
        "strategy.orb.reward_risk": 2.5, "live.allowed_machine": "DESKTOP-X"}


@pytest.mark.parametrize("bad", ["noequals", "=1", "a.b="])
def test_parse_sets_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_sets([bad])


def test_robustness_cli_set_enables_filter_params(monkeypatch, tmp_path):
    import backtest.robustness as R
    from tests.test_strategy_orb_filter import FILTER, settings as orb_s
    s = orb_s({**FILTER, "enabled": False})
    monkeypatch.setattr(R, "load_settings", lambda: s)
    monkeypatch.setattr(R, "BacktestEngine", lambda *a, **k: None)
    monkeypatch.setattr(R, "load_bars_parquet", lambda *a, **k: ([], 0))
    captured = {}

    def fake_run(settings, make, data, split, params, **kw):
        captured.update(params=params, settings=settings)
        raise SystemExit(0)
    monkeypatch.setattr(R, "run_robustness", fake_run)
    with pytest.raises(SystemExit):
        R.main(["--bars", str(tmp_path), "--split", "2026-09-01", "--set", "strategy.orb.range_filter.enabled=true"])
    assert "strategy.orb.range_filter.max_ratio" in captured["params"]
    assert captured["settings"].get("strategy.orb.range_filter.enabled") is True
    assert s.get("strategy.orb.range_filter.enabled") is False              # bản gốc không bị sửa


def test_robustness_cli_unknown_key_refused(monkeypatch, tmp_path, capsys):
    import backtest.robustness as R
    from tests.test_strategy_orb_filter import settings as orb_s
    monkeypatch.setattr(R, "load_settings", lambda: orb_s())
    assert R.main(["--bars", str(tmp_path), "--split", "2026-09-01", "--set", "strategy.orb.typo=1"]) == 2


def test_engine_cli_set_applies(monkeypatch, tmp_path, session_cfg, cal):
    import backtest.engine as eng
    from tests.test_backtest_engine import bt_settings
    from tests.test_backtest_roll import D18, NOV, day_bars, write
    s = bt_settings(session_cfg)
    write(tmp_path / "b" / "x.parquet", day_bars(D18, NOV, 1350.0))
    monkeypatch.setattr(eng, "load_settings", lambda: s)
    seen = {}
    orig = eng.BacktestEngine.__init__

    def init(self, st, strategy, calendar=None, trading_contract_only=True):
        seen["slip"] = st.get("backtest.slippage_ticks")
        orig(self, st, strategy, calendar=cal, trading_contract_only=trading_contract_only)
    monkeypatch.setattr(eng.BacktestEngine, "__init__", init)
    rc = eng.main(["--bars", str(tmp_path / "b"), "--strategy", "tests.test_backtest_roll:Noop",
                   "--set", "backtest.slippage_ticks=2", "--out", str(tmp_path / "r.html")])
    assert rc == 0 and seen["slip"] == 2
    assert eng.main(["--bars", str(tmp_path / "b"), "--strategy", "tests.test_backtest_roll:Noop",
                     "--set", "backtest.nope=2", "--out", str(tmp_path / "r.html")]) == 2
