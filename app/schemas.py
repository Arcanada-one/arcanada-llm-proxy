"""Pydantic models for OpenAI ↔ Model Connector translation.

OpenAI inbound: /v1/chat/completions request envelope.
MC outbound: POST /connectors/{name}/execute body.
MC inbound: ConnectorResponse with usage/result/structured fields.
OpenAI outbound: ChatCompletion response envelope with x_arcanada extension.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ──────────────────────────── OpenAI inbound ──────────────────────────── #


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ResponseFormatJsonSchemaInner(BaseModel):
    name: str
    strict: bool = True
    schema_: dict[str, Any] = Field(alias="schema")
    model_config = ConfigDict(populate_by_name=True)


class ResponseFormat(BaseModel):
    type: Literal["text", "json_object", "json_schema"]
    json_schema: ResponseFormatJsonSchemaInner | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    max_tokens: int | None = None
    temperature: float | None = None
    response_format: ResponseFormat | None = None


# ──────────────────────────── MC outbound ──────────────────────────── #


class MCResponseFormat(BaseModel):
    type: Literal["json_object", "text"]


class MCExecuteRequest(BaseModel):
    prompt: str
    model: str | None = None
    systemPrompt: str | None = None
    maxTokens: int | None = None
    responseFormat: MCResponseFormat | None = None
    jsonSchema: dict[str, Any] | None = None
    timeout: int | None = None


# ──────────────────────────── MC inbound ──────────────────────────── #


class MCUsage(BaseModel):
    inputTokens: int = 0
    outputTokens: int = 0
    cachedInputTokens: int = 0
    reasoningOutputTokens: int = 0
    totalTokens: int = 0
    costUsd: float = 0.0


class MCError(BaseModel):
    type: str
    message: str
    retryable: bool = False
    recommendation: str | None = None


class MCResponse(BaseModel):
    id: str
    connector: str | None = None
    model: str | None = None
    result: str = ""
    structured: Any | None = None
    usage: MCUsage = MCUsage()
    latencyMs: int = 0
    queueWaitMs: int = 0
    status: Literal["success", "error"]
    error: MCError | None = None
    attempt: int = 1
    maxAttempts: int = 1


# ──────────────────────────── OpenAI outbound ──────────────────────────── #


FallbackReason = Literal["none", "429", "auth_error", "validation_error"]
RouteTaken = Literal["codex", "openrouter"]


class XArcanada(BaseModel):
    route_taken: RouteTaken
    fallback_reason: FallbackReason = "none"
    upstream_latency_ms: int = 0
    upstream_request_id: str | None = None


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatCompletionMessage
    finish_reason: Literal["stop", "length", "error"] = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage
    x_arcanada: XArcanada
