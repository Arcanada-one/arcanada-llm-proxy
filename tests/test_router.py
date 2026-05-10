"""Routing decision matrix tests.

Truth table:
  json_schema → codex (with jsonSchema body, responseFormat=json_object)
  json_object → openrouter (with responseFormat=json_object)
  text        → openrouter (no responseFormat)
  absent      → openrouter (no responseFormat)
"""

import pytest

from app.router import route
from app.schemas import (
    ChatCompletionRequest,
    ChatMessage,
    ResponseFormat,
    ResponseFormatJsonSchemaInner,
)


def _req(rf: ResponseFormat | None = None, model: str = "openrouter/auto") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[
            ChatMessage(role="system", content="be concise"),
            ChatMessage(role="user", content="ping"),
        ],
        max_tokens=10,
        response_format=rf,
    )


def test_json_schema_routes_to_codex_with_jsonschema_body():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}
    rf = ResponseFormat(
        type="json_schema",
        json_schema=ResponseFormatJsonSchemaInner(name="t", strict=True, **{"schema": schema}),
    )
    connector, payload = route(_req(rf))
    assert connector == "codex"
    assert payload.jsonSchema == schema
    assert payload.responseFormat is not None
    assert payload.responseFormat.type == "json_object"
    # codex omits model under ChatGPT auth (CONN-0074)
    assert payload.model is None


def test_json_object_routes_to_openrouter():
    rf = ResponseFormat(type="json_object")
    connector, payload = route(_req(rf))
    assert connector == "openrouter"
    assert payload.jsonSchema is None
    assert payload.responseFormat is not None
    assert payload.responseFormat.type == "json_object"


def test_text_routes_to_openrouter_no_responseformat():
    rf = ResponseFormat(type="text")
    connector, payload = route(_req(rf))
    assert connector == "openrouter"
    assert payload.jsonSchema is None
    assert payload.responseFormat is None


def test_absent_response_format_routes_to_openrouter():
    connector, payload = route(_req(None))
    assert connector == "openrouter"
    assert payload.jsonSchema is None
    assert payload.responseFormat is None


def test_json_schema_without_schema_body_raises():
    rf = ResponseFormat(type="json_schema", json_schema=None)
    with pytest.raises(ValueError, match="json_schema"):
        route(_req(rf))


def test_messages_flattened_into_prompt_and_systemprompt():
    rf = ResponseFormat(type="json_object")
    req = ChatCompletionRequest(
        model="openrouter/auto",
        messages=[
            ChatMessage(role="system", content="rule"),
            ChatMessage(role="user", content="hello"),
            ChatMessage(role="assistant", content="hi"),
            ChatMessage(role="user", content="more"),
        ],
        response_format=rf,
    )
    _, payload = route(req)
    assert payload.systemPrompt == "rule"
    assert "hello" in payload.prompt
    assert "more" in payload.prompt


def test_openrouter_auto_model_resolves_to_default():
    _, payload = route(_req(None, model="openrouter/auto"))
    assert payload.model == "google/gemini-2.5-flash"


def test_openrouter_passthrough_model():
    _, payload = route(_req(None, model="anthropic/claude-haiku-4"))
    assert payload.model == "anthropic/claude-haiku-4"
