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
"""

from litellm.integrations.custom_logger import CustomLogger


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
        except Exception:
            # Never break a request from a sanitizer bug — fall through untouched.
            pass
        return data


proxy_handler_instance = AIServerHooks()
