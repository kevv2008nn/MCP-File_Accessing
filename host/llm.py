"""
LLM layer: ONE client for any OpenAI-compatible provider (LM Studio, Groq,
OpenRouter, Gemini's compatibility endpoint, ...). Switching provider means
changing three values in .env: LLM_BASE_URL, LLM_MODEL, LLM_API_KEY.
"""
import os
import uuid
from dataclasses import dataclass, field

import openai
from openai import AsyncOpenAI


class LLMError(Exception):
    """An error whose message is safe and helpful to show directly to the user."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str   # raw JSON string, exactly as the model produced it


@dataclass
class Turn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def as_message(self) -> dict:
        """The assistant message to store in the conversation history."""
        msg: dict = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            msg["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.name, "arguments": c.arguments}}
                                 for c in self.tool_calls]
        elif not self.text:
            msg["content"] = ""
        return msg


def provider_info() -> dict:
    """Safe to send to the browser: never includes the API key."""
    if os.environ.get("LLM_MODE") == "scripted":
        return {"mode": "scripted", "label": "Scripted test model (no AI)", "configured": True}
    base, model = os.environ.get("LLM_BASE_URL", ""), os.environ.get("LLM_MODEL", "")
    return {"mode": "openai-compatible", "base_url": base, "model": model,
            "label": f"{model} @ {base}" if base and model else "Not configured",
            "configured": bool(base and model)}


class LLM:
    def __init__(self, base_url: str, api_key: str, model: str, temperature: float = 0.2, timeout: float = 120):
        self.base_url, self.model, self.temperature = base_url, model, temperature
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key or "not-needed",
                                  timeout=timeout, max_retries=2)

    async def chat(self, messages: list[dict], tools: list[dict]) -> Turn:
        kwargs: dict = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except openai.AuthenticationError:
            raise LLMError("The provider rejected your API key (401). Check LLM_API_KEY in .env.")
        except openai.PermissionDeniedError as e:
            raise LLMError(f"Access denied (403): {_detail(e)}")
        except openai.NotFoundError:
            raise LLMError(f"Model {self.model!r} was not found at {self.base_url}. Check LLM_MODEL in .env.")
        except openai.RateLimitError:
            raise LLMError("Rate limit or free-tier quota reached (429). Wait a bit and try again, "
                           "or switch to another provider in .env.")
        except openai.APIConnectionError:
            raise LLMError(f"Cannot reach {self.base_url}. For LM Studio: start its local server and load a "
                           "model. For cloud providers: check your internet connection and LLM_BASE_URL.")
        except openai.BadRequestError as e:
            raise LLMError(f"The provider rejected the request (400): {_detail(e)}. If this mentions tools, "
                           "the model may not support function calling; try another model or disable a server.")
        except openai.APIStatusError as e:
            raise LLMError(f"Provider error {e.status_code}: {_detail(e)}")
        if not resp.choices:
            raise LLMError("The provider returned an empty response.")
        msg = resp.choices[0].message
        calls = []
        for tc in (msg.tool_calls or []):
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            calls.append(ToolCall(id=tc.id or f"call_{uuid.uuid4().hex[:8]}", name=fn.name,
                                  arguments=fn.arguments or "{}"))
        return Turn(text=(msg.content or "").strip(), tool_calls=calls)


def _detail(e: Exception) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"])[:300]
    return str(getattr(e, "message", e))[:300]


def make_llm():
    """Create the LLM from environment variables (loaded from .env)."""
    if os.environ.get("LLM_MODE") == "scripted":
        from .scripted_llm import ScriptedLLM
        return ScriptedLLM()
    base, model = os.environ.get("LLM_BASE_URL", ""), os.environ.get("LLM_MODEL", "")
    if not base or not model:
        raise LLMError("No model configured. Copy .env.example to .env and set LLM_BASE_URL, LLM_MODEL "
                       "and LLM_API_KEY, then restart.")
    return LLM(base, os.environ.get("LLM_API_KEY", ""), model,
               temperature=float(os.environ.get("LLM_TEMPERATURE", "0.2")))
