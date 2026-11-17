"""Python warnings are log records at WARNING; one cosmetic warning is quieted.

A warning the process did not route through logging is printed raw to stderr,
and a log pipeline that reads levels from structured lines files a raw stderr
line at ERROR. One library emits a pydantic serializer warning about its usage
object on every streamed call, so about 20,000 of them a fortnight arrived at
ERROR and buried the real errors. The structured setup now captures warnings
into logging, where each is a ``py.warnings`` record at WARNING in the normal
format, and drops that one known-cosmetic warning to DEBUG. Every other
warning is still logged.
"""

from __future__ import annotations

import io
import json
import logging
import warnings

import pytest
import structlog

from robothor.logging_setup import configure_structured_logging

SERIALIZER_WARNING = (
    "Pydantic serializer warnings:\n"
    "  PydanticSerializationUnexpectedValue(Expected `ResponseAPIUsage` - serialized value "
    "may not be as expected [input_value={'completion_tokens': 1}, input_type=dict])"
)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch):
    """The process's structured setup, writing JSON into a buffer."""
    monkeypatch.setenv("ROBOTHOR_LOG_FORMAT", "json")
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    buffer = io.StringIO()
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        configure_structured_logging()
        handler = root.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        handler.setStream(buffer)
        yield buffer
        logging.captureWarnings(False)
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    structlog.reset_defaults()


def _records(buffer: io.StringIO) -> list[dict]:
    out = []
    for line in buffer.getvalue().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def test_a_warning_is_a_structured_record_at_warning(captured) -> None:
    warnings.warn("example deprecation", DeprecationWarning, stacklevel=1)
    (record,) = [r for r in _records(captured) if "example deprecation" in r.get("event", "")]
    assert record["level"] == "warning"
    assert record["logger"] == "py.warnings"


def test_the_cosmetic_serializer_warning_is_not_logged_at_info(captured) -> None:
    warnings.warn(SERIALIZER_WARNING, UserWarning, stacklevel=1)
    assert not [r for r in _records(captured) if "ResponseAPIUsage" in r.get("event", "")]


def test_another_serializer_warning_is_still_logged(captured) -> None:
    other = SERIALIZER_WARNING.replace("ResponseAPIUsage", "ExampleModel")
    warnings.warn(other, UserWarning, stacklevel=1)
    rows = [r for r in _records(captured) if "ExampleModel" in r.get("event", "")]
    assert rows and rows[0]["level"] == "warning"
