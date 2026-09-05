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
"""

from litellm.integrations.custom_logger import CustomLogger

# Marker that prefixes every Open WebUI auto-task prompt template.
_OWUI_TASK_MARKER = "### Task:"


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
        return data


proxy_handler_instance = AIServerHooks()
