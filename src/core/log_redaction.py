"""
Keeps secrets out of every log output (.reaper.log, the UI log panel, the rate-limit status line).

Libraries log URLs that carry secrets, e.g. fluxer.py logs the full webhook URL, token included, on every
rate limit ("Rate limited on https://api.fluxer.app/v1/webhooks/<id>/<token>, ..."). We don't patch libraries,
so records are redacted when they are created, before any handler or filter sees them.
"""
import logging
import re

REDACTED = "<redacted>"

_PATTERNS = [
    # Webhook URLs (Fluxer, Discord and anything using the same route): /webhooks/<id>/<token>
    # (tokens are base64url: letters, digits, "-", "_")
    (re.compile(r"(/webhooks/\d+/)[A-Za-z0-9_\-]+"), rf"\1{REDACTED}"),
    # Authorization headers / values: "Authorization: Bot <token>", {'Authorization': '<token>'}
    (re.compile(r"(authorization['\"]?\s*[:=]\s*['\"]?(?:bot\s+|bearer\s+)?)[^\s'\",}]+", re.IGNORECASE),
     rf"\1{REDACTED}"),
]


def redact_secrets(text: str) -> str:
    """Returns text with webhook tokens and authorization values replaced by <redacted>."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def install_log_redaction() -> None:
    """Wraps the log record factory so every record's message (and traceback) is redacted. Safe to call twice."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_reaper_redacting", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:
            return record  # malformed args: leave it to logging's own error handling
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            # Formatters reuse exc_text, so the traceback is only rendered (and redacted) once
            record.exc_text = redact_secrets(logging.Formatter().formatException(record.exc_info))
        return record

    factory._reaper_redacting = True
    logging.setLogRecordFactory(factory)
