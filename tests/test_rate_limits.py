"""Rate-limit regressions with mocked completions, clocks, and sleeps only."""

from copy import deepcopy
from datetime import UTC, datetime
from email.utils import format_datetime
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from openai import APIStatusError, APITimeoutError, RateLimitError

from agent.llm import openai_compat
from agent.llm.openai_compat import OpenAICompatLLM, _retry_after_seconds


def rate_limit(message="Rate limited", headers=None):
    response = httpx.Response(
        429,
        headers=headers,
        request=httpx.Request("POST", "https://example.invalid/chat/completions"),
    )
    return RateLimitError(message, response=response, body={"error": {"message": message}})


def completion():
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="Done", tool_calls=None),
            finish_reason="stop",
        )],
        usage=None,
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("Rate-limit tests must not call a live API")
    monkeypatch.setattr(httpx.Client, "send", reject)


@pytest.mark.parametrize("message,expected", [
    ("Please try again in 24.4s", 24.4),
    ("Please TRY AGAIN IN 6.5S.", 6.5),
    ("Try again in 20 seconds", 20),
    ("Try again in .5 sec", 0.5),
    ("Retry again in 1 minute", 60),
    ("TRY AGAIN IN 1.5 MINUTES", 90),
    ("Try again in 1m2.5s", 62.5),
    ("Try again in 1m 2.5s", 62.5),
    ("Try again in 1 MIN and 2 SECONDS", 62),
    ("Try again in 0m10s", 10),
    ("Try again in 250ms", 0.25),
    ("Try again in 250 milliseconds", 0.25),
    ("Try again in 0s", 0),
    ("Used 3563, Requested 4633. Please try again in 8.972s.", 8.972),
])
def test_provider_retry_duration_strings(message, expected):
    assert _retry_after_seconds(rate_limit(message)) == pytest.approx(expected)


@pytest.mark.parametrize("message", [
    "Rate limit exceeded",
    "try again in -5s",
    "try again in NaNs",
    "try again in infinity seconds",
    "try again in 1 mystery unit",
    "try again in 1secondhand",
])
def test_invalid_provider_duration_is_ignored(message):
    assert _retry_after_seconds(rate_limit(message)) is None


@pytest.mark.parametrize("header,expected", [("12", 12), ("12.5", 12.5), ("0", 0), (" 9 ", 9)])
def test_retry_after_numeric_header_has_precedence(header, expected):
    error = rate_limit("try again in 30s", {"rEtRy-AfTeR": header})
    assert _retry_after_seconds(error) == expected


@pytest.mark.parametrize("header", ["invalid", "-1", "NaN", "inf", "-inf", ""])
def test_invalid_header_falls_back_to_provider_message(header):
    assert _retry_after_seconds(rate_limit("TRY AGAIN IN 12s", {"Retry-After": header})) == 12


def test_retry_after_http_date(monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(openai_compat.time, "time", lambda: now)
    retry_at = format_datetime(datetime.fromtimestamp(now + 45, UTC), usegmt=True)
    assert _retry_after_seconds(rate_limit("try again in 2s", {"Retry-After": retry_at})) == 45


def test_past_retry_after_http_date_does_not_produce_negative_sleep(monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(openai_compat.time, "time", lambda: now)
    retry_at = format_datetime(datetime.fromtimestamp(now - 45, UTC), usegmt=True)
    assert _retry_after_seconds(rate_limit(headers={"Retry-After": retry_at})) == 0


def mocked_llm(monkeypatch, outcomes, *, max_retries=5):
    pending = iter(outcomes)
    requests = []
    sleeps = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        outcome = next(pending)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    llm = OpenAICompatLLM.__new__(OpenAICompatLLM)
    llm.model = "test-model"
    llm.tool_mode = "native"
    llm.max_retries = max_retries
    monkeypatch.setattr(llm, "client", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), raising=False)
    monkeypatch.setattr(openai_compat.time, "sleep", sleeps.append)
    return llm, requests, sleeps


def test_repeated_short_429_hints_escalate(monkeypatch):
    llm, requests, sleeps = mocked_llm(monkeypatch, [rate_limit("try again in 0.1s")] * 4 + [completion()])
    assert llm.chat([]).content == "Done"
    assert sleeps == [4, 8, 16, 32]
    assert len(requests) == 5


def test_repeated_header_delays_escalate_from_actual_wait(monkeypatch):
    llm, _, sleeps = mocked_llm(monkeypatch, [rate_limit(headers={"Retry-After": "20"})] * 3 + [completion()])
    llm.chat([])
    assert sleeps == [20, 40, 80]


def test_backoff_is_bounded_without_early_retry(monkeypatch):
    llm, requests, sleeps = mocked_llm(monkeypatch, [rate_limit()] * 8 + [completion()], max_retries=9)
    llm.chat([])
    assert sleeps == [4, 8, 16, 32, 64, 120, 120, 120]
    assert len(requests) == 9


@pytest.mark.parametrize("error", [
    rate_limit(headers={"Retry-After": "121"}),
    rate_limit("TRY AGAIN IN 3 MINUTES"),
])
def test_excessive_server_delay_fails_instead_of_retrying_too_soon(monkeypatch, error):
    llm, requests, sleeps = mocked_llm(monkeypatch, [error, completion()])
    with pytest.raises(RuntimeError, match="retry wait limit; retry later") as failure:
        llm.chat([])
    assert failure.value.__cause__ is error
    assert sleeps == []
    assert len(requests) == 1


def test_no_sleep_after_final_429(monkeypatch):
    error = rate_limit("try again in 1s")
    llm, requests, sleeps = mocked_llm(monkeypatch, [error] * 3, max_retries=3)
    with pytest.raises(RuntimeError, match="after 3 attempts") as failure:
        llm.chat([])
    assert failure.value.__cause__ is error
    assert sleeps == [4, 8]
    assert len(requests) == 3


def test_single_attempt_never_sleeps(monkeypatch):
    llm, _, sleeps = mocked_llm(monkeypatch, [rate_limit()], max_retries=1)
    with pytest.raises(RuntimeError, match="after 1 attempts"):
        llm.chat([])
    assert sleeps == []


@pytest.mark.parametrize("other_failure", [
    APITimeoutError(request=httpx.Request("POST", "https://example.invalid")),
    APIStatusError("Service unavailable", response=httpx.Response(503, request=httpx.Request("POST", "https://example.invalid")), body=None),
])
def test_non_429_failure_resets_consecutive_rate_limit_backoff(monkeypatch, other_failure):
    llm, _, sleeps = mocked_llm(monkeypatch, [rate_limit(), rate_limit(), other_failure, rate_limit(), completion()])
    llm.chat([])
    assert sleeps == [4, 8, 8, 4]


def test_new_chat_starts_with_fresh_backoff(monkeypatch):
    llm, _, sleeps = mocked_llm(monkeypatch, [rate_limit(), rate_limit(), completion(), rate_limit(), completion()])
    llm.chat([])
    llm.chat([])
    assert sleeps == [4, 8, 4]


def test_retry_keeps_entire_tool_result_group_and_request_unchanged(monkeypatch):
    messages = [
        {"role": "system", "content": "Enforce refund policy"},
        {"role": "user", "content": "Refund order 1042"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "payments__get_charge", "arguments": '{"charge_id":1}'}},
            {"id": "call_2", "type": "function", "function": {"name": "payments__list_refunds", "arguments": '{"charge_id":1}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"id":1,"amount":3200}'},
        {"role": "tool", "tool_call_id": "call_2", "content": '[]'},
    ]
    tools = [{"type": "function", "function": {"name": "payments__get_charge", "parameters": {"type": "object"}}}]
    original_messages = deepcopy(messages)
    original_tools = deepcopy(tools)
    llm, requests, sleeps = mocked_llm(monkeypatch, [rate_limit(), completion()])
    llm.chat(messages, tools=tools, json_schema={"type": "object"}, temperature=0.2)
    assert len(sleeps) == 1
    assert requests[0] == requests[1]
    assert requests[1]["messages"] == original_messages
    assert requests[1]["tools"] == original_tools
    assert requests[1]["temperature"] == 0.2
    assert requests[1]["response_format"] == {"type": "json_object"}
    assert messages == original_messages
    assert tools == original_tools
