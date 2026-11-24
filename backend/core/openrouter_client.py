"""OpenRouter client wrapper using official openai SDK with strict JSON, retries, and cost tracking."""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import httpx
from openai import OpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from backend.config import Settings, get_settings
from backend.core.diagnostics import (
    EDITORIAL_MODEL_FALLBACKS,
    EDITORIAL_STRONG_MODEL_FALLBACKS,
    FAST_MODEL_FALLBACKS,
    VISION_MODEL_FALLBACKS,
)
from backend.models.job import CostMetrics, ModelUsageRecord


# Approximate USD per 1M tokens fallback table if OpenRouter models cache is offline
_FALLBACK_PRICING_PER_1M: dict[str, tuple[float, float]] = {
    "google/gemini-2.5-flash-lite": (0.10, 0.40),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4.1-nano": (0.10, 0.40),
    "openai/gpt-4.1-mini": (0.40, 1.60),
    "deepseek/deepseek-chat-v3-0324": (0.25, 1.00),
    "deepseek/deepseek-v3.2": (0.26, 0.38),
    "google/gemini-2.5-pro": (1.25, 10.0),
    "anthropic/claude-sonnet-4.5": (3.0, 15.0),
    "openai/gpt-4.1": (2.0, 8.0),
}


class TransientOpenRouterError(RuntimeError):
    """Retryable OpenRouter / JSON parsing error."""


class CostTracker:
    """Thread-safe accumulator for OpenRouter token counts and USD/EUR cost."""

    def __init__(self, initial: Optional[CostMetrics] = None, usd_to_eur: float = 0.92) -> None:
        self._lock = threading.Lock()
        self.metrics = initial.model_copy(deep=True) if initial else CostMetrics(usd_to_eur_rate=usd_to_eur)

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
    ) -> None:
        with self._lock:
            total_tok = prompt_tokens + completion_tokens
            self.metrics.total_calls += 1
            self.metrics.prompt_tokens += prompt_tokens
            self.metrics.completion_tokens += completion_tokens
            self.metrics.total_tokens += total_tok
            self.metrics.cost_usd = round(self.metrics.cost_usd + cost_usd, 6)
            self.metrics.cost_eur = round(self.metrics.cost_usd * self.metrics.usd_to_eur_rate, 6)

            rec = self.metrics.by_model.get(model)
            if rec is None:
                rec = ModelUsageRecord(model=model)
                self.metrics.by_model[model] = rec
            rec.calls += 1
            rec.prompt_tokens += prompt_tokens
            rec.completion_tokens += completion_tokens
            rec.total_tokens += total_tok
            rec.cost_usd = round(rec.cost_usd + cost_usd, 6)

    def snapshot(self) -> CostMetrics:
        with self._lock:
            return self.metrics.model_copy(deep=True)


def _parse_json_lenient(raw_text: str) -> dict[str, Any]:
    """Parse JSON object from model output, stripping markdown fences or stray prose."""
    text = (raw_text or "").strip()
    if not text:
        raise TransientOpenRouterError("La respuesta del modelo está vacía.")

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
    except json.JSONDecodeError:
        pass

    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        snippet = text[first : last + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise TransientOpenRouterError(f"JSON inválido recibido del modelo: {text[:240]}")


class OpenRouterClient:
    """Client for OpenRouter via openai SDK with automatic fallback, retries, and pricing."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        cost_tracker: Optional[CostTracker] = None,
        cancel_check: Optional[Callable[[], None]] = None,
        call_log_cb: Optional[Callable[[str], None]] = None,
        telemetry_cb: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.openrouter_api_key:
            raise RuntimeError("Falta OPENROUTER_API_KEY en el archivo .env.")

        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self.settings.openrouter_api_key,
            default_headers={
                "HTTP-Referer": "http://localhost:8000",
                "X-Title": "PodcastForge Studio",
            },
            max_retries=0,
            timeout=httpx.Timeout(connect=10.0, read=45.0, write=15.0, pool=10.0),
        )
        self.cost_tracker = cost_tracker or CostTracker()
        self._cancel_check = cancel_check
        self._call_log_cb = call_log_cb
        self._telemetry_cb = telemetry_cb
        self._pricing_cache: dict[str, tuple[float, float]] = {}
        self._load_pricing_table()

    def _raise_if_cancelled(self) -> None:
        if self._cancel_check is not None:
            self._cancel_check()

    @staticmethod
    def _reasoning_params_for_model(model: str, max_tokens: int) -> tuple[int, Optional[dict[str, Any]]]:
        """Keep reasoning tokens bounded so mandatory-reasoning models never starve JSON output."""
        low = (model or "").lower()
        if any(tag in low for tag in ("gemini-2.5-pro", "gemini-3", "deepseek-r1", "/o1", "/o3", "/o4")):
            budget = min(768, max(256, max_tokens // 3))
            return max_tokens + budget, {"reasoning": {"max_tokens": budget}}
        if "gemini-2.5-flash" in low and "lite" not in low and "image" not in low:
            return max_tokens, {"reasoning": {"max_tokens": 0}}
        return max_tokens, None

    def _load_pricing_table(self) -> None:
        cache_path = self.settings.processing_dir / "_openrouter_pricing.json"
        now = time.time()

        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if now - float(data.get("fetched_at", 0)) < 86400:
                    self._pricing_cache = {
                        k: (float(v[0]), float(v[1]))
                        for k, v in data.get("models", {}).items()
                    }
                    if self._pricing_cache:
                        return
            except Exception:
                pass

        try:
            resp = httpx.get("https://openrouter.ai/api/v1/models", timeout=10.0)
            if resp.status_code == 200:
                payload = resp.json().get("data", [])
                table: dict[str, list[float]] = {}
                for item in payload:
                    mid = item.get("id")
                    pricing = item.get("pricing") or {}
                    try:
                        p_in = float(pricing.get("prompt", 0.0)) * 1_000_000.0
                        p_out = float(pricing.get("completion", 0.0)) * 1_000_000.0
                        if mid:
                            table[mid] = [p_in, p_out]
                            self._pricing_cache[mid] = (p_in, p_out)
                    except (TypeError, ValueError):
                        continue
                if table:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(
                        json.dumps({"fetched_at": now, "models": table}),
                        encoding="utf-8",
                    )
        except Exception:
            pass

    def _estimate_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        usage_obj: Any,
    ) -> float:
        # Prefer exact cost reported by OpenRouter if available
        if usage_obj is not None:
            extra = getattr(usage_obj, "model_extra", None) or {}
            if isinstance(extra, dict) and extra.get("cost") is not None:
                try:
                    return float(extra["cost"])
                except (TypeError, ValueError):
                    pass
            direct_cost = getattr(usage_obj, "cost", None)
            if direct_cost is not None:
                try:
                    return float(direct_cost)
                except (TypeError, ValueError):
                    pass

        p_in, p_out = self._pricing_cache.get(
            model,
            _FALLBACK_PRICING_PER_1M.get(model, (0.20, 0.80)),
        )
        return (prompt_tokens / 1_000_000.0) * p_in + (completion_tokens / 1_000_000.0) * p_out

    def _candidate_models(
        self,
        role: Literal["fast", "editorial", "editorial_strong"],
        vision: bool = False,
    ) -> list[str]:
        if vision:
            if role == "fast":
                primary = self.settings.openrouter_model_fast
            elif role == "editorial_strong":
                primary = self.settings.openrouter_model_editorial_strong
            else:
                primary = self.settings.openrouter_model_editorial
            # Exclude text-only models (e.g. deepseek) from vision multimodal calls
            raw_list = [primary, *VISION_MODEL_FALLBACKS]
            seen_v: list[str] = []
            for m in raw_list:
                if not m:
                    continue
                if "deepseek" in m.lower():
                    continue
                if m not in seen_v:
                    seen_v.append(m)
            return seen_v or list(VISION_MODEL_FALLBACKS)

        if role == "fast":
            primary = self.settings.openrouter_model_fast
            fallbacks = FAST_MODEL_FALLBACKS
        elif role == "editorial_strong":
            primary = self.settings.openrouter_model_editorial_strong
            fallbacks = EDITORIAL_STRONG_MODEL_FALLBACKS
        else:
            primary = self.settings.openrouter_model_editorial
            fallbacks = EDITORIAL_MODEL_FALLBACKS
        seen: list[str] = []
        for m in [primary, *fallbacks]:
            if m and m not in seen:
                seen.append(m)
        return seen

    @retry(
        retry=retry_if_exception_type(TransientOpenRouterError),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1.0, min=1.0, max=4.0),
        reraise=True,
    )
    def _call_single_model(
        self,
        model: str,
        system_prompt: str,
        user_content: Any,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        self._raise_if_cancelled()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        eff_max_tokens, extra_body = self._reasoning_params_for_model(model, max_tokens)

        last_exc: Optional[Exception] = None
        use_reasoning_cfg = extra_body is not None
        for use_json_format in (True, False):
            while True:
                self._raise_if_cancelled()
                try:
                    kwargs: dict[str, Any] = {
                        "model": model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": eff_max_tokens,
                    }
                    if use_json_format:
                        kwargs["response_format"] = {"type": "json_object"}
                    if use_reasoning_cfg and extra_body is not None:
                        kwargs["extra_body"] = extra_body

                    t0 = time.monotonic()
                    resp = self.client.chat.completions.create(**kwargs)
                    elapsed_s = time.monotonic() - t0
                    choice = resp.choices[0]
                    finish_reason = getattr(choice, "finish_reason", None)
                    content = choice.message.content or ""
                    try:
                        parsed = _parse_json_lenient(content)
                    except TransientOpenRouterError:
                        if finish_reason == "length":
                            raise RuntimeError(
                                f"Modelo {model} truncó la respuesta JSON (finish_reason=length)."
                            )
                        raise

                    usage = resp.usage
                    p_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
                    c_tok = int(getattr(usage, "completion_tokens", 0) or 0)
                    cost = self._estimate_cost(model, p_tok, c_tok, usage)
                    self.cost_tracker.record(model, p_tok, c_tok, cost)
                    if self._telemetry_cb is not None:
                        try:
                            self._telemetry_cb(
                                {
                                    "type": "llm_call",
                                    "model": model,
                                    "prompt_tokens": p_tok,
                                    "completion_tokens": c_tok,
                                    "cost_usd": round(cost, 6),
                                    "duration_sec": round(elapsed_s, 3),
                                    "finish_reason": str(finish_reason or ""),
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                }
                            )
                        except Exception:
                            pass
                    if self._call_log_cb is not None:
                        try:
                            self._call_log_cb(
                                f"LLM {model}: {p_tok:,} in / {c_tok:,} out (${cost:.5f})"
                            )
                        except Exception:
                            pass
                    return parsed
                except TransientOpenRouterError:
                    self._raise_if_cancelled()
                    raise
                except Exception as exc:
                    self._raise_if_cancelled()
                    msg = str(exc)
                    low_msg = msg.lower()
                    last_exc = exc
                    if use_reasoning_cfg and ("reasoning" in low_msg or "extra_body" in low_msg):
                        use_reasoning_cfg = False
                        continue
                    if use_json_format and ("response_format" in low_msg or "json_object" in low_msg):
                        break
                    if any(code in low_msg for code in ("429", "500", "502", "503", "504", "timeout", "timed out")):
                        raise TransientOpenRouterError(msg) from exc
                    raise

        raise RuntimeError(str(last_exc))

    def _execute_with_fallback(
        self,
        role: Literal["fast", "editorial", "editorial_strong"],
        system_prompt: str,
        user_content: Any,
        temperature: float,
        max_tokens: int,
        err_prefix: str,
        vision: bool = False,
    ) -> dict[str, Any]:
        models = self._candidate_models(role, vision=vision)
        last_error: Optional[Exception] = None
        for idx_m, model in enumerate(models):
            self._raise_if_cancelled()
            try:
                return self._call_single_model(
                    model=model,
                    system_prompt=system_prompt,
                    user_content=user_content,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:
                self._raise_if_cancelled()
                last_error = exc
                msg = str(exc).lower()
                if "401" in msg or "invalid api key" in msg or "unauthorized" in msg:
                    raise RuntimeError(f"Error de autenticación en OpenRouter: {exc}") from exc
                if self._telemetry_cb is not None and idx_m + 1 < len(models):
                    try:
                        self._telemetry_cb(
                            {
                                "type": "llm_fallback",
                                "role": role,
                                "from_model": model,
                                "to_model": models[idx_m + 1],
                                "error": str(exc)[:300],
                                "ts": datetime.now(timezone.utc).isoformat(),
                            }
                        )
                    except Exception:
                        pass
                if self._call_log_cb is not None and idx_m + 1 < len(models):
                    try:
                        self._call_log_cb(
                            f"Fallback OpenRouter ({role}): {model} falló ({str(exc)[:90]}), probando {models[idx_m + 1]}"
                        )
                    except Exception:
                        pass
                continue
        raise RuntimeError(f"{err_prefix} '{role}': {last_error}")

    def chat_json(
        self,
        role: Literal["fast", "editorial", "editorial_strong"],
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Execute a strict-JSON chat completion with model fallback on unavailability."""
        return self._execute_with_fallback(
            role=role,
            system_prompt=system_prompt,
            user_content=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            err_prefix="Todos los modelos fallaron para rol",
            vision=False,
        )

    def chat_vision_json(
        self,
        role: Literal["fast", "editorial", "editorial_strong"],
        system_prompt: str,
        user_text: str,
        image_jpegs: list[bytes],
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Execute a multimodal vision + text JSON call using base64 JPEG data URLs."""
        content_parts: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for raw_bytes in image_jpegs:
            b64 = base64.b64encode(raw_bytes).decode("ascii")
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}",
                        "detail": "low",
                    },
                }
            )

        return self._execute_with_fallback(
            role=role,
            system_prompt=system_prompt,
            user_content=content_parts,
            temperature=temperature,
            max_tokens=max_tokens,
            err_prefix="Todos los modelos vision fallaron para rol",
            vision=True,
        )
