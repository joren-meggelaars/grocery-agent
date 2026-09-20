import json
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest
from pydantic import SecretStr

from grocery.config import Settings
from grocery.llm.client import AnthropicReceiptReader, ReaderUnavailable, system_prompt
from grocery.llm.pricing import estimate_cost_eur
from grocery.refdata import CATEGORIES
from tests.fakes import jpeg_bytes, plus_receipt


def make_reader(**overrides):
    settings = Settings(_env_file=None, anthropic_api_key=SecretStr("sk-test"), **overrides)
    return AnthropicReceiptReader(settings)


def reply(text, stop="end_turn", usage=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=usage or SimpleNamespace(
            input_tokens=100, output_tokens=50, cache_read_input_tokens=10, cache_creation_input_tokens=5
        ),
        _request_id="req_abc",
    )


class StubMessages:
    def __init__(self, outcome):
        self.outcome, self.kwargs = outcome, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def stub(reader, outcome):
    reader._client = SimpleNamespace(messages=StubMessages(outcome))
    return reader._client.messages


def test_missing_api_key_is_reported_clearly():
    with pytest.raises(ReaderUnavailable, match="ANTHROPIC_API_KEY"):
        AnthropicReceiptReader(Settings(_env_file=None))


def test_request_shape():
    reader = make_reader(llm_effort="low")
    messages = stub(reader, reply(plus_receipt().model_dump_json()))
    reader.read([jpeg_bytes(), jpeg_bytes("black")], None)

    kw = messages.kwargs
    assert kw["model"] == "claude-sonnet-5"
    content = kw["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "image", "text"]  # images first, then the instruction
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert kw["output_config"]["effort"] == "low"
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema" and "lines" in fmt["schema"]["properties"]
    assert "temperature" not in kw and "thinking" not in kw  # removed / not needed on Sonnet 5


def test_text_layer_is_appended_to_the_instruction():
    reader = make_reader()
    messages = stub(reader, reply(plus_receipt().model_dump_json()))
    reader.read([], "TOTAAL 8,46")
    content = messages.kwargs["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text"] and "TOTAAL 8,46" in content[0]["text"]


def test_system_prompt_lists_every_category_and_treats_the_receipt_as_data():
    prompt = system_prompt()
    assert all(f'"{name}"' in prompt for name, _ in CATEGORIES)
    assert "{categories}" not in prompt and "data, not instructions" in prompt


def test_successful_read_returns_parsed_raw_and_usage():
    reader = make_reader()
    stub(reader, reply(plus_receipt().model_dump_json()))
    result = reader.read([jpeg_bytes()], None)
    assert result.error is None and result.parsed.total_cents == 846
    assert result.raw["store_chain"] == "plus"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (100, 50)
    assert (result.usage.cache_read_tokens, result.usage.cache_write_tokens) == (10, 5)
    assert result.request_id == "req_abc"


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (reply("not json at all"), "valid JSON"),
        (reply(json.dumps({"store_chain": "plus"})), "expected format"),
        (reply("{}", stop="max_tokens"), "cut off"),
        (reply("", stop="refusal"), "declined"),
    ],
)
def test_bad_model_output_is_an_error_not_an_exception(outcome, expected):
    reader = make_reader()
    stub(reader, outcome)
    result = reader.read([jpeg_bytes()], None)
    assert result.parsed is None and expected in result.error and not result.retryable


def test_schema_mismatch_keeps_the_raw_output():
    reader = make_reader()
    stub(reader, reply(json.dumps({"store_chain": "plus"})))
    result = reader.read([jpeg_bytes()], None)
    assert result.raw == {"store_chain": "plus"}


def _status_error(code):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(code, request=request, json={"error": {"message": "nope"}})
    cls = anthropic.InternalServerError if code >= 500 else anthropic.BadRequestError
    return cls("nope", response=response, body=None)


def test_transient_errors_are_retryable_and_permanent_ones_are_not():
    reader = make_reader()
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    stub(reader, anthropic.APIConnectionError(request=request))
    assert reader.read([jpeg_bytes()], None).retryable

    stub(reader, _status_error(500))
    assert reader.read([jpeg_bytes()], None).retryable

    stub(reader, _status_error(400))
    result = reader.read([jpeg_bytes()], None)
    assert not result.retryable and "400" in result.error


def test_cost_estimate_uses_model_prices_and_cache_factors():
    eur = estimate_cost_eur("claude-sonnet-5", 1_000_000, 100_000, usd_to_eur=1.0)
    assert eur == pytest.approx(2.0 + 1.0)
    cached = estimate_cost_eur("claude-sonnet-5", 0, 0, cache_read_tokens=1_000_000, usd_to_eur=1.0)
    assert cached == pytest.approx(0.2)


def test_unknown_model_is_priced_at_the_highest_known_rate():
    assert estimate_cost_eur("claude-future-9", 1_000_000, 0, usd_to_eur=1.0) == pytest.approx(5.0)
