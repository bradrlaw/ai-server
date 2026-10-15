"""LiteLLM proxy pre-call hooks for the AI server.

sanitize_empty_assistant
-------------------------
llama.cpp's OpenAI-compatible server rejects any assistant message that has
neither a ``content`` key nor ``tool_calls`` with:

    400 - Assistant message must contain either 'content' or 'tool_calls'!

Some agentic clients (e.g. Copilot CLI BYOK) can persist a *content-less*
assistant turn into their transcript — typically when the upstream model
errored or returned an empty body mid-session. Every subsequent request then
replays that poisoned turn and 400s the whole session, even though the message
is harmless.

This hook normalizes such messages by giving any content-less, tool_call-less
assistant message an empty-string ``content`` (which llama-server accepts),
making the stack resilient to one bad turn without the user losing context.

disable_thinking_for_owui_tasks
-------------------------------
Open WebUI runs background "task" calls (chat title, tags, follow-up
suggestions, retrieval/search query generation) against the *current chat
model*. Our chat models are Qwen3 reasoning models, so each trivial task
triggers a full multi-thousand-token thinking phase (~40-65s each). Chained
after a message, several of these produce the long post-response "churn".

Every Open WebUI auto-task prompt begins with the literal ``### Task:`` header
(DEFAULT_{TITLE,TAGS,FOLLOW_UP,QUERY}_GENERATION_PROMPT_TEMPLATE all start with
it). We detect that marker and inject ``chat_template_kwargs.enable_thinking =
False`` so the task is answered directly, with no thinking phase. This keeps
tasks on the user's chosen model (no task-model routing, no model swap) and
preserves all Open WebUI features — it only removes the wasteful reasoning on
these throwaway prompts. The kwarg is a harmless no-op for non-Qwen templates
(Jinja ignores unused context variables).

sanitize_schemas_for_llama_grammar
----------------------------------
llama.cpp compiles tool-parameter and ``response_format`` JSON schemas into a
sampling grammar, and two of its limits reject schemas that real clients send.
Either one 400s the whole request. OpenClaw's owner-only ``cron`` tool hits
both, which broke every OpenClaw Control UI chat on the Qwen slots:

1. ``pattern`` must be written as ``^...$`` (``json-schema-to-grammar.cpp``
   ``_visit_pattern``). JSON Schema patterns are *search* patterns and are
   normally unanchored (TypeBox, zod and pydantic emit them that way)::

       Unable to generate parser for this template. Automatic parser
       generation failed: JSON schema conversion failed: Pattern must start
       with '^' and end with '$'

   Anchoring instead would change the meaning (``\\S`` = "contains a
   non-space"; ``^\\S$`` = "exactly one character"), so the keyword is
   dropped. cron: ``job.declarationKey`` ``pattern "\\S"``.

2. A grammar repetition ``{m,n}`` must stay below 2000 (``llama-grammar.cpp``
   ``MAX_REPETITION_THRESHOLD``). String ``minLength``/``maxLength`` and array
   ``minItems``/``maxItems`` compile to exactly such repetitions::

       Failed to initialize samplers: failed to parse grammar

   (llama-server log: "number of repetitions exceeds sane defaults"). The
   converter's wrapper rules count too: measured on build 9850, strings fail
   from ``maxLength`` 2000, arrays with ``minItems`` 0 from ``maxItems`` 1999.
   Bounds of 1000 or more are dropped, not clamped: clamping would make the
   grammar cut long arguments short, while dropping only lifts the limit from
   the grammar. cron: ``trigger.script`` ``maxLength 65536``.
   Length bounds are only touched where llama.cpp compiles them (``type``
   includes ``"string"``); on a typeless top-level parameter they are what
   marks it string-like, so they stay.

The hook removes only those keywords, so the grammar gets looser, never
stricter. OpenClaw re-validates every tool call against its own schema
(``validateToolArguments``) and returns violations to the model as tool
errors. Each distinct removal is logged once. Every model behind this gateway is
llama.cpp; gate this by deployment if a hosted model is ever added. Not
handled (never seen in tool schemas): a large ``{m,n}`` quantifier inside an
anchored pattern, or nested repetitions inside one pattern whose product
reaches 2000.
"""

import logging

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("LiteLLM Proxy")

# Marker that prefixes every Open WebUI auto-task prompt template.
_OWUI_TASK_MARKER = "### Task:"

# Keywords whose value maps names to subschemas: their keys are property/def
# names (a property may itself be called "pattern"), never schema keywords.
_SCHEMA_MAP_KEYWORDS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies"}
)
# Keywords whose values are instance data or non-schema metadata (possibly
# keyed by property names) — never rewritten.
_SCHEMA_DATA_KEYWORDS = frozenset(
    {"enum", "const", "default", "examples", "example", "dependentRequired", "discriminator"}
)
# llama-grammar.cpp MAX_REPETITION_THRESHOLD is 2000, but the converter's
# wrapper rules count against it too: strings fail from maxLength 2000, arrays
# with minItems 0 already from maxItems 1999. Half the limit leaves margin.
_GRAMMAR_BOUND_LIMIT = 1000
_logged_drops = set()


def _is_string_schema(node):
    t = node.get("type")
    return t == "string" or (isinstance(t, list) and "string" in t)


def _too_many_repetitions(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= _GRAMMAR_BOUND_LIMIT


def _sanitize_schema(node, path, found, schema_map=False):
    """Recursively delete llama.cpp-incompatible keywords in place."""
    if isinstance(node, dict):
        if not schema_map:
            if "pattern" in node:
                pat = node["pattern"]
                # Non-strings are invalid JSON Schema and also make llama.cpp throw.
                if not (isinstance(pat, str) and pat.startswith("^") and pat.endswith("$")):
                    del node["pattern"]
                    found.append((path or "/", "pattern", pat))
            bounds = ["minItems", "maxItems"]
            if _is_string_schema(node):
                bounds += ["minLength", "maxLength"]
            for kw in bounds:
                if kw in node and _too_many_repetitions(node[kw]):
                    found.append((path or "/", kw, node.pop(kw)))
        for key, val in node.items():
            sub = f"{path}/{key}"
            if schema_map:
                _sanitize_schema(val, sub, found)
            elif key in _SCHEMA_MAP_KEYWORDS:
                _sanitize_schema(val, sub, found, schema_map=True)
            elif key not in _SCHEMA_DATA_KEYWORDS:
                _sanitize_schema(val, sub, found)
    elif isinstance(node, list):
        for i, val in enumerate(node):
            _sanitize_schema(val, f"{path}/{i}", found)


def _schema_targets(data):
    """Yield (label, schema) for every JSON schema llama.cpp turns into a grammar."""
    tools = data.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function")
            if isinstance(fn, dict):  # Chat Completions
                yield f"tool {fn.get('name')}", fn.get("parameters")
            else:  # Responses API (flat) / Anthropic Messages
                for key in ("parameters", "input_schema"):
                    yield f"tool {tool.get('name')}", tool.get(key)
    rf = data.get("response_format")
    if isinstance(rf, dict):
        js = rf.get("json_schema")
        yield "response_format", js.get("schema") if isinstance(js, dict) else None
        yield "response_format", rf.get("schema")  # llama.cpp json_object+schema form


def _sanitize_schemas_for_llama_grammar(data):
    for label, schema in _schema_targets(data):
        if not isinstance(schema, (dict, list)):
            continue
        found = []
        _sanitize_schema(schema, "", found)
        for path, kw, val in found:
            key = (label, path, kw, repr(val))
            if key not in _logged_drops and len(_logged_drops) < 512:
                _logged_drops.add(key)
                if kw == "pattern":
                    why = "llama.cpp only accepts ^...$-anchored patterns"
                else:
                    why = f"llama.cpp grammar repetitions; dropping bounds >= {_GRAMMAR_BOUND_LIMIT}"
                _log.warning(
                    "custom_hooks: dropped JSON-schema %s %r at %s %s (%s)",
                    kw, val, label, path, why,
                )


def _is_owui_task(messages):
    """True if any message looks like an Open WebUI background task prompt."""
    if not isinstance(messages, list):
        return False
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("user", "system"):
            continue
        content = m.get("content")
        if isinstance(content, str) and content.lstrip().startswith(_OWUI_TASK_MARKER):
            return True
        # Multimodal content: list of parts with {"type": "text", "text": ...}.
        if isinstance(content, list):
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                    and part["text"].lstrip().startswith(_OWUI_TASK_MARKER)
                ):
                    return True
    return False


class AIServerHooks(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            messages = data.get("messages")
            if isinstance(messages, list):
                for m in messages:
                    if (
                        isinstance(m, dict)
                        and m.get("role") == "assistant"
                        and not m.get("tool_calls")
                        and m.get("content") is None
                    ):
                        # Covers both a missing key and an explicit null.
                        m["content"] = ""

            # Suppress the reasoning phase on Open WebUI background task calls.
            if _is_owui_task(messages):
                kwargs = data.get("chat_template_kwargs")
                if not isinstance(kwargs, dict):
                    kwargs = {}
                # Don't override an explicit caller-provided value.
                kwargs.setdefault("enable_thinking", False)
                data["chat_template_kwargs"] = kwargs
        except Exception:
            # Never break a request from a sanitizer bug — fall through untouched.
            pass
        try:
            _sanitize_schemas_for_llama_grammar(data)
        except Exception:
            pass
        return data


proxy_handler_instance = AIServerHooks()
