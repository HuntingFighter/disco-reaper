"""Secrets (webhook tokens, authorization values) never reach a log handler."""
import logging

import pytest

from src.core.log_redaction import install_log_redaction, redact_secrets

TOKEN = "3V7kaIPgtNndMQ1dpq1DmJFkhpunJGInOD4aOwpts1m2cCZVYqC16eWzYlRKHib6"
URL = f"https://api.fluxer.app/v1/webhooks/1570920240236199936/{TOKEN}"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


@pytest.fixture
def capture():
    original = logging.getLogRecordFactory()
    install_log_redaction()
    install_log_redaction()  # idempotent: must not wrap twice
    handler = _Capture()
    logger = logging.getLogger("fluxer.http")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(old_level)
    logging.setLogRecordFactory(original)


def test_fluxer_rate_limit_warning_is_redacted(capture):
    # The exact call fluxer.py makes on a 429
    logging.getLogger("fluxer.http").warning("Rate limited on %s, retry in %.2fs (attempt %d)", URL, 0.43, 1)
    line = capture.lines[-1]
    assert TOKEN not in line
    assert line == "Rate limited on https://api.fluxer.app/v1/webhooks/1570920240236199936/<redacted>, retry in 0.43s (attempt 1)"


def test_exception_text_and_traceback_are_redacted(capture):
    capture.setFormatter(logging.Formatter("%(message)s"))
    try:
        raise RuntimeError(f"Failed after 6 attempts: POST {URL}?wait=true")
    except RuntimeError as e:
        logging.getLogger("fluxer.http").exception(f"send failed: {e}")
    assert TOKEN not in capture.lines[-1]
    assert "webhooks/1570920240236199936/<redacted>?wait=true" in capture.lines[-1]
    assert "Traceback" in capture.lines[-1]


def test_unrelated_messages_are_untouched(capture):
    logging.getLogger("fluxer.http").info("Created channel %s with %d%% done", "general", 50)
    assert capture.lines[-1] == "Created channel general with 50% done"


@pytest.mark.parametrize("text, expected", [
    (f"https://discord.com/api/webhooks/123/{TOKEN}", "https://discord.com/api/webhooks/123/<redacted>"),
    (f"headers={{'Authorization': 'Bot {TOKEN}'}}", "headers={'Authorization': 'Bot <redacted>'}"),
    (f"Authorization: {TOKEN}", "Authorization: <redacted>"),
    ("GET /channels/123/webhooks", "GET /channels/123/webhooks"),  # webhook list route has no token
])
def test_redact_secrets_patterns(text, expected):
    assert redact_secrets(text) == expected
