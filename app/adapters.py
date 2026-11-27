"""Outside services: the LLM (Claude or Gemini), Twilio SMS and owner alerts (Slack + email),
with fakes for testing.

Both LLM clients expose the same create(system, tools, messages, timeout) method and return the
same response shape (stop_reason + content blocks of type "text" / "tool_use"), so the agent loop,
prompt, tools and guardrails are identical whichever provider you pick with LLM_PROVIDER."""
import copy
import json
import logging
import re
import smtplib
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


def make_llm(settings) -> Any:
    """Pick the LLM client from LLM_PROVIDER ("gemini" or "anthropic")."""
    provider = (settings.llm_provider or "gemini").lower()
    if provider == "gemini":
        return GeminiLLM(settings.gemini_api_key, settings.gemini_model, settings.gemini_thinking_budget,
                         thinking_level=settings.gemini_thinking_level)
    if provider == "anthropic":
        return AnthropicLLM(settings.anthropic_api_key, settings.anthropic_model)
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r} (use 'gemini' or 'anthropic')")


def llm_key_name(settings) -> tuple[str, str]:
    """(env var name, value) of the API key the chosen provider needs."""
    if (settings.llm_provider or "gemini").lower() == "anthropic":
        return "ANTHROPIC_API_KEY", settings.anthropic_api_key
    return "GEMINI_API_KEY", settings.gemini_api_key


# ---------- Gemini (google-genai SDK) ----------

# JSON Schema keywords passed to Gemini as-is. Everything else (pattern, maxLength, format...)
# is turned into a plain-English hint in the description. The server still enforces every
# constraint: tool inputs are validated by pydantic in tool_handlers.py before anything runs.
_KEEP_KEYS = {"type", "description", "properties", "required", "enum", "items", "additionalProperties"}


def gemini_schema(schema: dict) -> dict:
    out, hints = {}, []
    for key, value in schema.items():
        if key == "properties":
            out[key] = {name: gemini_schema(sub) for name, sub in value.items()}
        elif key == "items":
            out[key] = gemini_schema(value)
        elif key in _KEEP_KEYS:
            out[key] = value
        elif key == "pattern" and value == "^[0-9]{5}$":
            hints.append("Exactly 5 digits.")
        elif key == "pattern":
            hints.append(f"Must match the pattern {value}.")
        elif key == "maxLength":
            hints.append(f"At most {value} characters.")
        elif key == "minLength":
            hints.append(f"At least {value} characters.")
        elif key == "format" and value == "date":
            hints.append("Format: YYYY-MM-DD.")
        elif key == "format":
            hints.append(f"Must be a valid {value}.")
    if hints:
        out["description"] = (out.get("description", "") + " " + " ".join(hints)).strip()
    return out


_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="gemini")
_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s")


def _api_timeout_ms(seconds: float) -> int:
    """Gemini rejects request deadlines under 10 s, so never send less than 11 s."""
    return int(max(seconds, 10) * 1000) + 1000


def retry_delay_seconds(exc: Exception) -> Optional[float]:
    """Google's suggested wait from a 429 error (RetryInfo.retryDelay), if present."""
    m = _RETRY_DELAY_RE.search(str(getattr(exc, "details", "")) + " " + str(exc))
    return float(m.group(1)) if m else None


class GeminiLLM:
    """Gemini through the official google-genai SDK, translated to and from the Anthropic-style
    messages the agent uses. Tool calls run one at a time under our control (the SDK's automatic
    function calling is turned off), exactly like the Claude path."""

    def __init__(self, api_key: str, model: str, thinking_budget: int = 0, client: Any = None,
                 thinking_level: str = "low"):
        if client is None:
            from google import genai
            client = genai.Client(api_key=api_key)
        self.client = client
        self.model = model
        self.thinking_budget = thinking_budget
        self.thinking_level = (thinking_level or "").strip().upper()
        # Gemini attaches "thought signatures" to function-call parts. They must be sent back
        # unchanged on the next request of the same tool loop, so keep the original parts.
        self._call_parts: "OrderedDict[str, Any]" = OrderedDict()
        self._native_ids: dict[str, bool] = {}
        self._decl_cache: dict[int, list] = {}

    # -- public --

    def create(self, system: list[dict], tools: list[dict], messages: list[dict],
               timeout: float, max_tokens: int = 600) -> Any:
        from google.genai import types

        is_25 = self.model.startswith("gemini-2.5")
        # Gemini 2.5 uses a token budget (0 = off). Gemini 3+ uses a level and always thinks a
        # little, so leave room for those tokens or the visible reply gets cut off.
        thinking = max(int(self.thinking_budget or 0), 0) if is_25 else 2048
        config = types.GenerateContentConfig(
            system_instruction="\n\n".join(b["text"] for b in system if b.get("text")),
            tools=[types.Tool(function_declarations=self._declarations(tools))],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO")),
            max_output_tokens=max_tokens + thinking,
            http_options=types.HttpOptions(timeout=_api_timeout_ms(timeout)),
        )
        if is_25:
            # 0 = no thinking: fastest, and keeps the first text inside the 10-second promise.
            config.thinking_config = types.ThinkingConfig(thinking_budget=thinking)
        elif self.thinking_level:
            # LOW keeps replies fast enough for the 10-second first text.
            config.thinking_config = types.ThinkingConfig(thinking_level=self.thinking_level)
        return self._to_response(self._generate(self._to_contents(messages), config, timeout))

    # -- request translation --

    def _declarations(self, tools: list[dict]) -> list:
        from google.genai import types
        key = id(tools)
        if key not in self._decl_cache:
            self._decl_cache[key] = [
                types.FunctionDeclaration(name=t["name"], description=t["description"],
                                          parameters_json_schema=gemini_schema(t["input_schema"]))
                for t in tools  # cache_control (an Anthropic-only key) is simply not copied
            ]
        return self._decl_cache[key]

    def _to_contents(self, messages: list[dict]) -> list:
        from google.genai import types
        names: dict[str, str] = {}
        contents = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            parts = []
            for b in blocks:
                if b["type"] == "text":
                    if b.get("text"):
                        parts.append(types.Part(text=b["text"]))
                elif b["type"] == "tool_use":
                    names[b["id"]] = b["name"]
                    original = self._call_parts.get(b["id"])
                    parts.append(original if original is not None else types.Part(
                        function_call=types.FunctionCall(name=b["name"], args=b["input"])))
                elif b["type"] == "tool_result":
                    call_id = b["tool_use_id"]
                    try:
                        payload = json.loads(b["content"])
                    except (TypeError, ValueError):
                        payload = {"result": b["content"]}
                    if not isinstance(payload, dict):
                        payload = {"result": payload}
                    parts.append(types.Part(function_response=types.FunctionResponse(
                        id=call_id if self._native_ids.get(call_id) else None,
                        name=names.get(call_id, "unknown"), response=payload)))
            if parts:
                contents.append(types.Content(role=role, parts=parts))
        return contents

    # -- call with one retry for rate limits / server hiccups (free tier hits 429s often) --

    def _generate(self, contents, config, timeout: float):
        """Call Gemini, enforcing OUR deadline (e.g. 7 s for the first text) client-side, since the
        API itself refuses deadlines under 10 s. Retries once on 429/5xx when Google's suggested
        wait still fits inside the deadline."""
        from google.genai import errors, types
        started = time.monotonic()

        def attempt(cfg, budget: float):
            future = _EXECUTOR.submit(self.client.models.generate_content,
                                      model=self.model, contents=contents, config=cfg)
            try:
                return future.result(timeout=max(budget, 0.1))
            except FuturesTimeout:
                raise TimeoutError(f"Gemini did not answer within {budget:.1f}s") from None

        try:
            return attempt(config, timeout)
        except errors.APIError as exc:
            left = timeout - (time.monotonic() - started)
            wait = retry_delay_seconds(exc) or 2.0
            if (exc.code == 429 or exc.code >= 500) and left - wait > 3:
                log.warning("Gemini returned %s; retrying once in %.1fs", exc.code, wait)
                time.sleep(wait)
                left = timeout - (time.monotonic() - started)
                config = copy.copy(config)
                config.http_options = types.HttpOptions(timeout=_api_timeout_ms(left))
                return attempt(config, left)
            raise

    # -- response translation --

    def _to_response(self, resp) -> SimpleNamespace:
        candidates = getattr(resp, "candidates", None) or []
        content_obj = candidates[0].content if candidates else None
        parts = (content_obj.parts if content_obj is not None else None) or []
        blocks = []
        for p in parts:
            if p.function_call is not None:
                fc = p.function_call
                call_id = fc.id or f"call_{uuid.uuid4().hex[:12]}"
                self._remember(call_id, p, bool(fc.id))
                blocks.append(SimpleNamespace(type="tool_use", id=call_id, name=fc.name,
                                              input=dict(fc.args or {})))
            elif p.text and not p.thought:
                blocks.append(SimpleNamespace(type="text", text=p.text))
        if not candidates:
            log.warning("Gemini returned no candidates (blocked or empty): %s",
                        getattr(resp, "prompt_feedback", None))
        has_tool = any(b.type == "tool_use" for b in blocks)
        return SimpleNamespace(stop_reason="tool_use" if has_tool else "end_turn", content=blocks)

    def _remember(self, call_id: str, part: Any, native: bool) -> None:
        self._call_parts[call_id] = part
        self._native_ids[call_id] = native
        while len(self._call_parts) > 1000:
            old, _ = self._call_parts.popitem(last=False)
            self._native_ids.pop(old, None)


# ---------- Claude ----------

class AnthropicLLM:
    def __init__(self, api_key: str, model: str):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=1)
        self.model = model

    def create(self, system: list[dict], tools: list[dict], messages: list[dict],
               timeout: float, max_tokens: int = 600) -> Any:
        return self.client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system, tools=tools,
            messages=messages, timeout=timeout,
        )


# ---------- SMS ----------

class OptedOutError(Exception):
    """Twilio refused the send because the number replied STOP (error 21610)."""


class TwilioSMS:
    def __init__(self, account_sid: str, auth_token: str):
        from twilio.rest import Client
        self.client = Client(account_sid, auth_token)

    def send(self, from_number: str, to_number: str, body: str) -> str:
        from twilio.base.exceptions import TwilioRestException
        try:
            msg = self.client.messages.create(from_=from_number, to=to_number, body=body)
            return msg.sid
        except TwilioRestException as exc:
            if exc.code == 21610:
                raise OptedOutError(to_number) from exc
            raise


@dataclass
class FakeSMS:
    sent: list[dict] = field(default_factory=list)
    opted_out_numbers: set = field(default_factory=set)
    echo: bool = False  # simulator prints texts as they're "sent"

    def send(self, from_number, to_number, body):
        if to_number in self.opted_out_numbers:
            raise OptedOutError(to_number)
        self.sent.append({"from": from_number, "to": to_number, "body": body})
        if self.echo:
            print(f"\n  AGENT ({len(body)} chars): {body}\n")
        return f"SM_fake_{len(self.sent)}"


# ---------- Owner alerts ----------

class AlertSender:
    def __init__(self, settings):
        self.s = settings

    def send(self, client_cfg: dict, title: str, text: str, urgent: bool = False) -> None:
        prefix = ":rotating_light: URGENT " if urgent else ""
        webhook = client_cfg.get("slack_webhook_url")
        if webhook:
            try:
                httpx.post(webhook, json={"text": f"{prefix}*{title}*\n{text}"}, timeout=10)
            except httpx.HTTPError:
                log.exception("Slack alert failed")
        owner_email = client_cfg.get("owner_email")
        if owner_email and self.s.smtp_host:
            try:
                msg = EmailMessage()
                msg["Subject"] = ("URGENT: " if urgent else "") + title
                msg["From"] = self.s.alert_from_email or self.s.smtp_user
                msg["To"] = owner_email
                msg.set_content(text)
                with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=15) as smtp:
                    smtp.starttls()
                    if self.s.smtp_user:
                        smtp.login(self.s.smtp_user, self.s.smtp_password)
                    smtp.send_message(msg)
            except Exception:
                log.exception("Email alert failed")
        if not webhook and not (owner_email and self.s.smtp_host):
            log.warning("No alert channel configured: %s\n%s", title, text)


@dataclass
class FakeAlerts:
    sent: list[dict] = field(default_factory=list)
    echo: bool = False

    def send(self, client_cfg, title, text, urgent=False):
        self.sent.append({"title": title, "text": text, "urgent": urgent})
        if self.echo:
            print(f"  [OWNER ALERT{' - URGENT' if urgent else ''}] {title}\n  {text.splitlines()[0] if text else ''}")


@dataclass
class Deps:
    """Everything the agent needs from the outside world. Swapped for fakes in tests."""
    llm: Any
    sms: Any
    alerts: Any
    calendar: Any
    settings: Any
    now: Any  # callable returning an aware UTC datetime
    extra: Optional[dict] = None
