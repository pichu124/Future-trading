import json

import pandas as pd
import pyarrow.parquet as pq
import pytest

from utils import atomic_io


def test_write_dataframe_roundtrip_and_no_tmp_left(tmp_path):
    out = tmp_path / "sub" / "bars.parquet"
    df = pd.DataFrame({"symbol": ["VN30F2611"] * 2, "close": [1350.1, 1350.4]})
    atomic_io.write_dataframe_atomic(df, out)
    back = pq.read_table(out).to_pandas()
    pd.testing.assert_frame_equal(back, df)
    assert [p.name for p in out.parent.iterdir()] == ["bars.parquet"]


def test_json_roundtrip_and_default(tmp_path):
    out = tmp_path / "state.json"
    assert atomic_io.read_json(out, default={"x": 1}) == {"x": 1}
    atomic_io.write_json_atomic({"tiếng": "Việt", "n": 2}, out)
    assert atomic_io.read_json(out) == {"tiếng": "Việt", "n": 2}
    assert "Việt" in out.read_text(encoding="utf-8")     # ensure_ascii=False


def test_unserializable_payload_never_truncates_old_file(tmp_path):
    out = tmp_path / "state.json"
    atomic_io.write_json_atomic({"ok": True}, out)

    class Boom:
        def __str__(self):
            raise ValueError("không serialize được")

    with pytest.raises(ValueError):
        atomic_io.write_json_atomic({"bad": Boom()}, out)
    assert json.loads(out.read_text(encoding="utf-8")) == {"ok": True}
    assert len(list(tmp_path.iterdir())) == 1


def test_replace_retries_transient_permission_error(tmp_path, monkeypatch):
    calls = {"n": 0}
    real_replace = atomic_io.os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("file đang bị khoá")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", flaky)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda s: None)
    atomic_io.write_json_atomic({"a": 1}, tmp_path / "x.json")
    assert calls["n"] == 3


def test_replace_gives_up_and_cleans_tmp(tmp_path, monkeypatch):
    def always_locked(src, dst):
        raise PermissionError("khoá mãi")

    monkeypatch.setattr(atomic_io.os, "replace", always_locked)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        atomic_io.write_json_atomic({"a": 1}, tmp_path / "x.json")
    assert list(tmp_path.iterdir()) == []
