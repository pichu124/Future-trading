import pytest

from monitoring import telegram as tg

ENV = {"TELEGRAM_TRADING_BOT_TOKEN": "tok", "TELEGRAM_TRADING_CHAT_ID": "-100"}


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {"ok": True, "result": {}}
        self.text = str(self._body)

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        return self.responses.pop(0) if self.responses else FakeResp()


def test_credentials_fall_back_to_default_channel():
    env = {"TELEGRAM_BOT_TOKEN": "def", "TELEGRAM_CHAT_ID": "1", "TELEGRAM_TRADING_CHAT_ID": "2"}
    assert tg.credentials("trading", env) == ("def", "2")
    assert tg.credentials(None, env) == ("def", "1")


def test_sanitize_keeps_whitelisted_tags_only():
    out = tg.sanitize_html("<b>Lãi</b> 5 < 6 & <script>x</script>")
    assert out == "<b>Lãi</b> 5 &lt; 6 &amp; &lt;script&gt;x&lt;/script&gt;"
    assert tg.sanitize_html("<b>x</b>") == "<b>x</b>"


def test_split_lines_respects_limit_and_hard_cuts():
    text = "\n".join(["a" * 30] * 10)
    parts = tg.split_lines(text, 100)
    assert all(len(p) <= 100 for p in parts) and "\n".join(parts) == text
    assert tg.split_lines("x" * 250, 100) == ["x" * 100, "x" * 100, "x" * 50]


def test_long_message_sent_in_numbered_parts():
    s = FakeSession([])
    tg.send_message("\n".join(["dòng"] * 2000), session=s, env=ENV)
    assert len(s.calls) > 1
    assert s.calls[0]["text"].endswith(f"<i>(1/{len(s.calls)})</i>")
    assert all(len(c["text"]) <= tg.MAX_MSG_LEN for c in s.calls)


def test_html_rejected_falls_back_to_plain():
    s = FakeSession([FakeResp(400, {"description": "can't parse entities"}), FakeResp()])
    tg.send_message("<b>Cảnh báo</b>", session=s, env=ENV)
    assert s.calls[1]["text"] == "Cảnh báo" and "parse_mode" not in s.calls[1]


def test_supergroup_migration_followed():
    s = FakeSession([FakeResp(400, {"description": "migrated",
                                    "parameters": {"migrate_to_chat_id": -1009}}), FakeResp()])
    tg.send_message("hi", session=s, env=ENV)
    assert s.calls[1]["chat_id"] == -1009


def test_missing_config_raises_but_notify_swallows():
    with pytest.raises(tg.TelegramConfigError):
        tg.send_message("x", env={})
    assert tg.notify("x", env={}) is False


def test_http_error_raises():
    s = FakeSession([FakeResp(401, {"description": "Unauthorized"})])
    with pytest.raises(RuntimeError, match="401"):
        tg.send_message("x", session=s, env=ENV, parse_mode="")
