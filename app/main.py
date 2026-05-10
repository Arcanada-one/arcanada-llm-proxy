"""FastAPI entry — OpenAI-compat /v1/chat/completions over Model Connector.

Architecture:
  client (Graphiti / ARCA / Cognee) --OpenAI--> proxy --MC HTTP--> model-connector :3900

Fallback: codex 429 / auth_error → openrouter retry, surface fallback_reason in
response.x_arcanada extension.
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status

from .auth import require_bearer, set_shared_secret
from .mc_client import MCClient
from .observability import init_tracing, request_span
from .router import route
from .schemas import (
    ChatCompletionChoice,
    ChatCompletionMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionUsage,
    FallbackReason,
    MCExecuteRequest,
    MCResponse,
    RouteTaken,
    XArcanada,
)
from .vault import load_shared_secret

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("proxy")

VERSION = "0.1.0"

mc_client = MCClient()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    set_shared_secret(load_shared_secret())
    init_tracing()
    log.info("arcanada-llm-proxy %s ready", VERSION)
    try:
        yield
    finally:
        await mc_client.aclose()


app = FastAPI(title="arcanada-llm-proxy", version=VERSION, lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": VERSION}


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    body: ChatCompletionRequest,
    _auth: None = Depends(require_bearer),
) -> ChatCompletionResponse:
    try:
        connector, mc_payload = route(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response, route_taken, fallback_reason = await _execute_with_fallback(connector, mc_payload)

    with request_span(
        {
            "route_taken": route_taken,
            "fallback_reason": fallback_reason,
            "input_tokens": response.usage.inputTokens,
            "cached_input_tokens": response.usage.cachedInputTokens,
            "output_tokens": response.usage.outputTokens,
            "reasoning_output_tokens": response.usage.reasoningOutputTokens,
            "upstream_latency_ms": response.latencyMs,
        }
    ):
        pass

    if response.status == "error":
        err = response.error
        msg = err.message if err else "upstream failure"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY if not (err and err.retryable) else 503,
            detail=msg,
        )

    return _to_openai_response(body.model, response, route_taken, fallback_reason)


async def _execute_with_fallback(
    connector: RouteTaken, payload: MCExecuteRequest
) -> tuple[MCResponse, RouteTaken, FallbackReason]:
    response = await mc_client.execute(connector, payload)

    fallback_reason: FallbackReason = "none"
    if response.status == "error" and connector == "codex":
        err_type = response.error.type if response.error else ""
        if err_type in ("rate_limited", "auth_error", "execution_error"):
            log.warning("codex %s — falling back to openrouter", err_type)
            # On fallback we drop the strict-mode json_schema contract: OpenRouter
            # without a schema-aware system prompt cannot guarantee a JSON object,
            # and MC's json_object validator rejects free-text. Forcing text mode
            # keeps the request alive at the cost of losing structured-output
            # guarantees. Schema-on-fallback enhancement is a backlog item.
            payload = payload.model_copy(update={"jsonSchema": None, "model": None, "responseFormat": None})
            response = await mc_client.execute("openrouter", payload)
            fallback_reason = "429" if err_type == "rate_limited" else "auth_error"
            connector = "openrouter"

    return response, connector, fallback_reason


def _to_openai_response(
    requested_model: str,
    mc: MCResponse,
    route_taken: RouteTaken,
    fallback_reason: FallbackReason,
) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
        created=int(time.time()),
        model=mc.model or requested_model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatCompletionMessage(role="assistant", content=mc.result),
                finish_reason="stop",
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=mc.usage.inputTokens,
            completion_tokens=mc.usage.outputTokens,
            total_tokens=mc.usage.totalTokens,
        ),
        x_arcanada=XArcanada(
            route_taken=route_taken,
            fallback_reason=fallback_reason,
            upstream_latency_ms=mc.latencyMs,
            upstream_request_id=mc.id if mc.id != "synthetic" else None,
        ),
    )
