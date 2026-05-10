"""Routing logic: OpenAI request → (connector, MC payload).

Rules (PRD-CONN-0045 §"Phase 2"):
  - response_format.type == "json_schema" → codex (Class B subscription, supports schema)
  - response_format.type in {"json_object", "text", None} → openrouter (per-token)
"""

from .schemas import ChatCompletionRequest, MCExecuteRequest, MCResponseFormat, RouteTaken


def _flatten_messages(messages: list) -> tuple[str, str | None]:
    """Collapse OpenAI messages array → (prompt, systemPrompt).

    System messages joined as systemPrompt; user/assistant content concatenated for prompt.
    """
    system_chunks: list[str] = []
    convo_chunks: list[str] = []
    for m in messages:
        if m.role == "system":
            system_chunks.append(m.content)
        else:
            convo_chunks.append(f"{m.role}: {m.content}" if m.role != "user" else m.content)
    prompt = "\n\n".join(convo_chunks).strip() or " "
    system_prompt = "\n\n".join(system_chunks).strip() or None
    return prompt, system_prompt


def route(req: ChatCompletionRequest) -> tuple[RouteTaken, MCExecuteRequest]:
    """Decide connector + build MC payload."""
    prompt, system_prompt = _flatten_messages(req.messages)
    rf = req.response_format

    json_schema_body: dict | None = None
    response_format: MCResponseFormat | None = None

    if rf is not None and rf.type == "json_schema":
        if rf.json_schema is None or not rf.json_schema.schema_:
            raise ValueError("response_format.type=json_schema requires json_schema.schema")
        json_schema_body = rf.json_schema.schema_
        response_format = MCResponseFormat(type="json_object")
        connector: RouteTaken = "codex"
    elif rf is not None and rf.type == "json_object":
        response_format = MCResponseFormat(type="json_object")
        connector = "openrouter"
    else:
        connector = "openrouter"

    payload = MCExecuteRequest(
        prompt=prompt,
        model=_resolve_model(req.model, connector),
        systemPrompt=system_prompt,
        maxTokens=req.max_tokens,
        responseFormat=response_format,
        jsonSchema=json_schema_body,
    )
    return connector, payload


_OPENROUTER_DEFAULT = "google/gemini-2.5-flash"
_CODEX_DEFAULT = "o4-mini"


def _resolve_model(requested: str, connector: RouteTaken) -> str | None:
    """Map OpenAI model name → MC connector model.

    Codex: account-bound under ChatGPT auth (CONN-0074) — must omit model.
    OpenRouter: pass-through; "openrouter/auto" → flash default.
    """
    if connector == "codex":
        return None
    if requested in ("openrouter/auto", "auto", ""):
        return _OPENROUTER_DEFAULT
    return requested
