"""Multi-provider LLM engine with cascade fallback and strict JSON via Instructor.

Architecture:

    +---------- FallbackLLMClient (priority chain) ----------+
    |                                                         |
    |   GeminiClient  →  OpenAIClient  →  AnthropicClient    |
    |       │                  │                  │           |
    |   per-provider     per-provider       per-provider     |
    |   AsyncRateLimiter AsyncRateLimiter   AsyncRateLimiter |
    |                                                         |
    +---------------------------------------------------------+

Error classification (this is the key to never freezing on a dead provider):

    - per-minute 429 (retryDelay < 90s)  → retry inside same provider.
    - 5xx / timeout / overloaded         → retry inside same provider.
    - HARD quota: 'PerDay', 'insufficient_quota', 'billing', retryDelay > 90s
      → mark provider in cooldown, fail over to the next provider in chain.
    - validation / auth / unknown        → re-raise immediately (no fallback).

Rate-limit safety:

    Each provider gets its OWN AsyncRateLimiter sized to its free-tier cap with
    a 5% safety margin. So even if you swap LLM_PROVIDER=openai today, you won't
    accidentally hit OpenAI's 3 RPM free cap with the rate limiter you tuned for
    Gemini's 15 RPM cap.

Selection (env vars):

    LLM_PROVIDERS=gemini,openai,anthropic   # priority chain (preferred)
    LLM_PROVIDER=gemini                     # legacy single-provider mode
    GEMINI_RATE_LIMIT_RPM, OPENAI_RATE_LIMIT_RPM, ANTHROPIC_RATE_LIMIT_RPM
    LLM_RATE_LIMIT_RPM                      # legacy fallback if per-provider unset

If no LLM_PROVIDERS / LLM_PROVIDER is set, the engine auto-builds a chain from
every provider that has a non-empty API key, in [gemini, openai, anthropic] order.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Protocol, Tuple

import instructor
from pydantic import ValidationError

from src.core.schema import Analysis, ThreadInput, TokenUsage

logger = logging.getLogger(__name__)

MAX_THREAD_MESSAGES = 6
PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.txt"

# Default safe RPMs under each provider's free tier (Apr 2026):
#   Gemini   2.5 Flash-Lite  : 15 RPM hard cap (free)  → 8  ≈ 47% margin
#   OpenAI   gpt-4.1-nano    : 3  RPM (no billing)     → 3  (assume worst case)
#   Claude   Haiku 4.5       : 5  RPM (free trial)     → 4  ≈ 20% margin
# Override with <PROVIDER>_RATE_LIMIT_RPM env vars.
_DEFAULT_RPM = {"gemini": 8.0, "openai": 3.0, "anthropic": 4.0}

# Cooldown applied to a provider when it raises a HARD quota error.
# 30 min covers per-day quotas resetting at the next UTC midnight slot for our
# typical use; the script likely finishes long before this matters.
_HARD_QUOTA_COOLDOWN_S = 30 * 60


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def render_thread(thread: ThreadInput) -> str:
    """Render the thread as a compact, model-friendly string.

    Includes only the last MAX_THREAD_MESSAGES messages. Country hint is appended
    at the end so the model can use it to fill `jurisdiction` when relevant.
    """
    messages = thread.messages[-MAX_THREAD_MESSAGES:]
    lines = [f"[{m.direction}] {m.text}" for m in messages]
    body = "\n".join(lines)
    suffix = ""
    if thread.country_hint:
        suffix += f"\n\n[CONTEXT] Customer country: {thread.country_hint}"
    if thread.detected_language:
        suffix += f"\n[CONTEXT] Pre-detected language: {thread.detected_language}"
    return f"<thread case_id={thread.case_id}>\n{body}\n</thread>{suffix}"


# ──────────────────────────────────────────────────────────────────────────────
#  Rate limiter (one instance per provider, shared across workers of that provider)
# ──────────────────────────────────────────────────────────────────────────────


class AsyncRateLimiter:
    """Coalesce-friendly rate limiter that enforces a min interval between calls.

    Shared across all concurrent workers of the SAME provider via a single
    instance. Uses a "next allowed at" timestamp instead of a token bucket
    because providers like Gemini measure RPM strictly (hard cap), not as a
    bucket that refills smoothly.
    """

    def __init__(self, max_per_minute: float):
        self.max_per_minute = max_per_minute
        self.interval = (60.0 / max_per_minute) * 1.05 if max_per_minute > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_allowed_at: float = 0.0

    async def acquire(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed_at = max(now, self._next_allowed_at) + self.interval

    async def penalize(self, seconds: float) -> None:
        """Push the next-allowed timestamp forward (used after a 429 response)."""
        async with self._lock:
            now = time.monotonic()
            self._next_allowed_at = max(self._next_allowed_at, now + seconds)


# ──────────────────────────────────────────────────────────────────────────────
#  Error classification
# ──────────────────────────────────────────────────────────────────────────────


_RETRY_DELAY_RE = re.compile(r"['\"]retryDelay['\"]:\s*['\"]([\d.]+)s['\"]")

_TRANSIENT_MARKERS = (
    "429", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED",
    "500 Internal", "502 Bad Gateway", "503 Service", "504 Gateway",
    "rate limit", "rate_limit", "overloaded", "timeout",
    # Gemini empty-response / safety-filter hiccups. The SDK tries to access
    # `response.candidates[0].content.parts` and crashes with AttributeError
    # when Gemini returns an empty payload (often a content-filter false
    # positive on certain customer messages). These are transient: a retry
    # almost always succeeds, sometimes with a slightly rephrased response.
    "object has no attribute 'parts'",
    "object has no attribute 'content'",
    "NoneType' object has no attribute",
)

# Any of these in an error message signals "this provider is dead for a while,
# don't bother retrying — fail over to the next provider in the chain".
_HARD_QUOTA_MARKERS = (
    "perday",                      # Gemini: GenerateRequestsPerDayPerProjectPerModel
    "per_day",
    "daily quota",
    "insufficient_quota",          # OpenAI: no billing / out of credits
    "exceeded your current quota", # OpenAI
    "billing",                     # OpenAI / Anthropic billing failures
    "credit balance",              # Anthropic: no credits left
    "free_tier_requests",          # Gemini quota metric
    "this api method requires billing", # OpenAI
)

# Heuristic: retry hints longer than this mean "wait until tomorrow", so we
# treat them as hard quota and fail over instead of sleeping.
_RETRY_DELAY_HARD_THRESHOLD_S = 90.0


def _extract_retry_delay(exc: BaseException) -> Optional[float]:
    """Try to extract a retry hint from a provider-specific 429 error."""
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        try:
            return max(0.5, float(retry_after))
        except (TypeError, ValueError):
            pass
    msg = str(exc)
    m = _RETRY_DELAY_RE.search(msg)
    if m:
        try:
            return max(0.5, float(m.group(1)))
        except ValueError:
            pass
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "rate" in msg.lower():
        return 5.0
    return None


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, ValidationError):
        return False
    msg = str(exc)
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def _is_hard_quota(exc: BaseException, retry_delay: Optional[float]) -> bool:
    """Return True if this provider is effectively unavailable for a long time."""
    msg = str(exc).lower()
    if any(m in msg for m in _HARD_QUOTA_MARKERS):
        return True
    if retry_delay is not None and retry_delay > _RETRY_DELAY_HARD_THRESHOLD_S:
        return True
    return False


class ProviderExhausted(Exception):
    """Raised by a single-provider client when its quota is dead.

    The FallbackLLMClient catches this to mark the provider in cooldown and
    proceed to the next one in the chain. Carries the original exception for
    logging.
    """

    def __init__(self, provider: str, original: BaseException, cooldown_s: float):
        super().__init__(f"{provider} exhausted: {original}")
        self.provider = provider
        self.original = original
        self.cooldown_s = cooldown_s


class AllProvidersExhausted(Exception):
    """Raised by FallbackLLMClient when EVERY provider in the chain is in cooldown.

    This is a *signal*, not a generic error — the batch script catches it to
    abort early instead of letting hundreds of remaining workers spam the same
    failure. `analyze_thread_safely` re-raises this specific exception so it
    can propagate up to the orchestrator/worker layer.
    """

    def __init__(self, chain_description: str):
        super().__init__(
            f"All providers in cooldown — wait or check quotas. Chain: {chain_description}"
        )
        self.chain_description = chain_description


# ──────────────────────────────────────────────────────────────────────────────
#  Retry wrapper
# ──────────────────────────────────────────────────────────────────────────────


async def _call_with_retry(
    fn: Callable[[], Awaitable],
    *,
    rate_limiter: Optional[AsyncRateLimiter] = None,
    max_attempts: int = 4,
    base_backoff: float = 2.0,
    label: str = "llm_call",
    provider_name: str = "unknown",
):
    """Run `fn` with rate limiting and retries for per-minute transient errors.

    On hard-quota errors (daily quota, insufficient_quota, billing, very long
    retryDelay) raises ProviderExhausted IMMEDIATELY so the caller (typically
    FallbackLLMClient) can fail over to the next provider instead of wasting
    retries against a dead provider.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        if rate_limiter is not None:
            await rate_limiter.acquire()
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_transient(exc):
                raise
            retry_delay = _extract_retry_delay(exc)
            if _is_hard_quota(exc, retry_delay):
                logger.warning(
                    "[%s] HARD quota on provider=%s — failing over. detail=%s",
                    label, provider_name, str(exc)[:240],
                )
                raise ProviderExhausted(provider_name, exc, _HARD_QUOTA_COOLDOWN_S) from exc

            if retry_delay is not None and rate_limiter is not None:
                await rate_limiter.penalize(retry_delay)
            sleep_for = retry_delay if retry_delay is not None else (
                base_backoff ** attempt + random.uniform(0, 1)
            )
            sleep_for = min(sleep_for, 60.0)
            logger.warning(
                "[%s] transient on provider=%s (attempt %d/%d) — sleeping %.1fs. detail=%s",
                label, provider_name, attempt, max_attempts, sleep_for, str(exc)[:240],
            )
            if attempt == max_attempts:
                # Exhausted retries on a per-minute issue → treat the provider
                # as temporarily unavailable so the chain can fail over.
                raise ProviderExhausted(provider_name, exc, 60.0) from exc
            await asyncio.sleep(sleep_for)
    assert last_exc is not None
    raise last_exc


# ──────────────────────────────────────────────────────────────────────────────
#  Provider clients
# ──────────────────────────────────────────────────────────────────────────────


class LLMClient(Protocol):
    provider_name: str
    model: str

    async def analyze(self, thread: ThreadInput) -> Tuple[Analysis, TokenUsage]: ...


class GeminiClient:
    provider_name = "gemini"

    def __init__(self, api_key: str, model: str, rate_limiter: Optional[AsyncRateLimiter] = None):
        from google import genai

        self.model = model
        self._raw_client = genai.Client(api_key=api_key)
        self._client = instructor.from_genai(
            self._raw_client, mode=instructor.Mode.GENAI_TOOLS
        )
        self._system_prompt = load_system_prompt()
        self._rate_limiter = rate_limiter

    async def analyze(self, thread: ThreadInput) -> Tuple[Analysis, TokenUsage]:
        rendered = render_thread(thread)

        # NOTE: `instructor.from_genai(...)` returns a SYNCHRONOUS client.
        # We push the blocking call to a worker thread so the event loop stays free.
        def _sync_call():
            return self._client.chat.completions.create_with_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": rendered},
                ],
                response_model=Analysis,
                max_retries=0,
            )

        async def _call():
            t0 = time.perf_counter()
            res, comp = await asyncio.to_thread(_sync_call)
            return res, comp, int((time.perf_counter() - t0) * 1000)

        result, completion, latency_ms = await _call_with_retry(
            _call,
            rate_limiter=self._rate_limiter,
            label=f"gemini[{thread.case_id}]",
            provider_name=self.provider_name,
        )
        usage = _extract_usage_gemini(completion)
        return result, TokenUsage(
            tokens_in=usage[0],
            tokens_out=usage[1],
            model=self.model,
            provider=self.provider_name,
            latency_ms=latency_ms,
        )


def _extract_usage_gemini(completion) -> Tuple[int, int]:
    try:
        meta = getattr(completion, "usage_metadata", None)
        if meta is None and isinstance(completion, dict):
            meta = completion.get("usage_metadata")
        if meta is None:
            return (0, 0)
        prompt = getattr(meta, "prompt_token_count", None)
        if prompt is None and isinstance(meta, dict):
            prompt = meta.get("prompt_token_count", 0)
        out = getattr(meta, "candidates_token_count", None)
        if out is None and isinstance(meta, dict):
            out = meta.get("candidates_token_count", 0)
        return int(prompt or 0), int(out or 0)
    except Exception:  # pragma: no cover
        return (0, 0)


class OpenAIClient:
    provider_name = "openai"

    def __init__(self, api_key: str, model: str, rate_limiter: Optional[AsyncRateLimiter] = None):
        from openai import AsyncOpenAI

        self.model = model
        self._raw_client = AsyncOpenAI(api_key=api_key)
        self._client = instructor.from_openai(self._raw_client)
        self._system_prompt = load_system_prompt()
        self._rate_limiter = rate_limiter

    async def analyze(self, thread: ThreadInput) -> Tuple[Analysis, TokenUsage]:
        rendered = render_thread(thread)

        async def _call():
            t0 = time.perf_counter()
            res, comp = await self._client.chat.completions.create_with_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": rendered},
                ],
                response_model=Analysis,
                max_retries=0,
            )
            return res, comp, int((time.perf_counter() - t0) * 1000)

        result, completion, latency_ms = await _call_with_retry(
            _call,
            rate_limiter=self._rate_limiter,
            label=f"openai[{thread.case_id}]",
            provider_name=self.provider_name,
        )
        u = getattr(completion, "usage", None)
        return result, TokenUsage(
            tokens_in=int(getattr(u, "prompt_tokens", 0) or 0) if u else 0,
            tokens_out=int(getattr(u, "completion_tokens", 0) or 0) if u else 0,
            model=self.model,
            provider=self.provider_name,
            latency_ms=latency_ms,
        )


class AnthropicClient:
    provider_name = "anthropic"

    def __init__(self, api_key: str, model: str, rate_limiter: Optional[AsyncRateLimiter] = None):
        from anthropic import AsyncAnthropic

        self.model = model
        self._raw_client = AsyncAnthropic(api_key=api_key)
        self._client = instructor.from_anthropic(self._raw_client)
        self._system_prompt = load_system_prompt()
        self._rate_limiter = rate_limiter

    async def analyze(self, thread: ThreadInput) -> Tuple[Analysis, TokenUsage]:
        rendered = render_thread(thread)

        async def _call():
            t0 = time.perf_counter()
            res, comp = await self._client.messages.create_with_completion(
                model=self.model,
                max_tokens=2048,
                system=self._system_prompt,
                messages=[{"role": "user", "content": rendered}],
                response_model=Analysis,
                max_retries=0,
            )
            return res, comp, int((time.perf_counter() - t0) * 1000)

        result, completion, latency_ms = await _call_with_retry(
            _call,
            rate_limiter=self._rate_limiter,
            label=f"anthropic[{thread.case_id}]",
            provider_name=self.provider_name,
        )
        u = getattr(completion, "usage", None)
        return result, TokenUsage(
            tokens_in=int(getattr(u, "input_tokens", 0) or 0) if u else 0,
            tokens_out=int(getattr(u, "output_tokens", 0) or 0) if u else 0,
            model=self.model,
            provider=self.provider_name,
            latency_ms=latency_ms,
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Fallback chain
# ──────────────────────────────────────────────────────────────────────────────


class FallbackLLMClient:
    """Wraps an ordered list of LLMClients with provider cooldown bookkeeping.

    Behavior on `analyze()`:
      1. Iterate clients in priority order.
      2. Skip any client whose cooldown_until > now.
      3. Try `client.analyze(thread)`:
           - success → return.
           - ProviderExhausted → mark provider in cooldown, try next.
           - any other exception → re-raise (no fallback for validation/auth bugs).
      4. If every client is in cooldown or all exhausted → raise the last error.

    The `provider_name` and `model` attributes report the *active* (next-to-try)
    provider, so logs and audit records always reflect what's actually serving.
    """

    def __init__(self, clients: List[LLMClient]):
        if not clients:
            raise RuntimeError("FallbackLLMClient requires at least one provider client.")
        self._clients = clients
        self._cooldown_until: dict[int, float] = {}
        self._lock = asyncio.Lock()

    @property
    def provider_name(self) -> str:
        active = self._first_available_index()
        if active is None:
            chain = "|".join(c.provider_name for c in self._clients)
            return f"fallback[{chain}:all_cooled_down]"
        return self._clients[active].provider_name

    @property
    def model(self) -> str:
        active = self._first_available_index()
        if active is None:
            return "n/a"
        return self._clients[active].model

    @property
    def chain_description(self) -> str:
        return " → ".join(
            f"{c.provider_name}({c.model})" for c in self._clients
        )

    def _first_available_index(self) -> Optional[int]:
        now = time.monotonic()
        for idx in range(len(self._clients)):
            if self._cooldown_until.get(idx, 0.0) <= now:
                return idx
        return None

    async def _mark_cooldown(self, idx: int, seconds: float) -> None:
        async with self._lock:
            self._cooldown_until[idx] = max(
                self._cooldown_until.get(idx, 0.0),
                time.monotonic() + seconds,
            )

    async def analyze(self, thread: ThreadInput) -> Tuple[Analysis, TokenUsage]:
        """Try providers in priority order, only failing over on hard quota.

        Cooldown policy (lesson learned the hard way):

          * `ProviderExhausted` (hard quota, billing exhausted, daily cap) →
            mark this provider in cooldown and try the next one. This is the
            ONLY case that triggers cooldown.
          * `ValidationError` → re-raise. A different provider won't fix bad
            schema output for the same input.
          * Anything else (`AttributeError` from Gemini empty response, sudden
            network blip, unknown SDK exception) → re-raise WITHOUT cooldown.

        The earlier behavior of cooling down on unknown errors caused a bad
        outage: a single Gemini content-filter glitch on one message would
        cool down the whole provider for 5min, which combined with OpenAI
        having no billing meant the next call hit `AllProvidersExhausted`
        and aborted ~50 healthy messages. Single-message failures should
        stay single-message failures — `analyze_thread_safely` turns them
        into `status=failed_llm` for that one row, and the batch keeps going.
        """
        last_exc: Optional[BaseException] = None
        now = time.monotonic()
        tried_any = False
        for idx, client in enumerate(self._clients):
            if self._cooldown_until.get(idx, 0.0) > now:
                continue
            tried_any = True
            try:
                return await client.analyze(thread)
            except ProviderExhausted as exc:
                last_exc = exc
                await self._mark_cooldown(idx, exc.cooldown_s)
                logger.warning(
                    "[fallback] provider=%s in cooldown for %.0fs (case=%s). "
                    "Trying next in chain...",
                    client.provider_name, exc.cooldown_s, thread.case_id,
                )
                continue
            except ValidationError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Unknown error on this single call. Do NOT cooldown — that
                # was the bug. Just re-raise so the worker marks this one
                # message as failed_llm and the next message gets a fresh
                # attempt against the same (still-healthy) provider.
                logger.warning(
                    "[fallback] non-quota error on provider=%s (case=%s): %s. "
                    "Re-raising (NOT cooling down — this is a single-call failure).",
                    client.provider_name, thread.case_id, str(exc)[:240],
                )
                raise

        if not tried_any:
            raise AllProvidersExhausted(self.chain_description)
        # Every provider tried in this call ended in cooldown → same signal.
        if isinstance(last_exc, ProviderExhausted) and self._first_available_index() is None:
            raise AllProvidersExhausted(self.chain_description) from last_exc
        assert last_exc is not None
        raise last_exc


# ──────────────────────────────────────────────────────────────────────────────
#  Factory
# ──────────────────────────────────────────────────────────────────────────────


def _provider_rpm(provider: str, override: Optional[float] = None) -> float:
    if override is not None and override > 0:
        return override
    env_specific = os.getenv(f"{provider.upper()}_RATE_LIMIT_RPM")
    if env_specific:
        try:
            return float(env_specific)
        except ValueError:
            logger.warning("Invalid %s_RATE_LIMIT_RPM=%r, using default.", provider.upper(), env_specific)
    legacy = os.getenv("LLM_RATE_LIMIT_RPM")
    if legacy:
        try:
            return float(legacy)
        except ValueError:
            pass
    return _DEFAULT_RPM.get(provider, 8.0)


def _build_single_client(provider: str, rate_limit_rpm: Optional[float] = None) -> Optional[LLMClient]:
    """Build one provider client if its API key is available; else return None."""
    rpm = _provider_rpm(provider, rate_limit_rpm)
    rate_limiter = AsyncRateLimiter(rpm) if rpm > 0 else None

    if provider == "gemini":
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            return None
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
        logger.info("Provider gemini ready: model=%s rpm=%.1f (interval %.2fs)",
                    model, rpm, rate_limiter.interval if rate_limiter else 0)
        return GeminiClient(api_key=key, model=model, rate_limiter=rate_limiter)

    if provider == "openai":
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            return None
        model = os.getenv("OPENAI_MODEL", "gpt-4.1-nano")
        logger.info("Provider openai ready: model=%s rpm=%.1f (interval %.2fs)",
                    model, rpm, rate_limiter.interval if rate_limiter else 0)
        return OpenAIClient(api_key=key, model=model, rate_limiter=rate_limiter)

    if provider == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            return None
        model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
        logger.info("Provider anthropic ready: model=%s rpm=%.1f (interval %.2fs)",
                    model, rpm, rate_limiter.interval if rate_limiter else 0)
        return AnthropicClient(api_key=key, model=model, rate_limiter=rate_limiter)

    raise RuntimeError(f"Unknown provider '{provider}'. Use gemini | openai | anthropic.")


def build_client_from_env(
    provider: Optional[str] = None,
    rate_limit_rpm: Optional[float] = None,
) -> LLMClient:
    """Build the active LLM client from environment variables.

    Resolution order:
      1. If `provider` argument is given → single-provider mode (backward compat
         with `process_batch.py --provider openai`).
      2. Else if `LLM_PROVIDERS` env is set → cascade chain in that order.
      3. Else if `LLM_PROVIDER` env is set → single-provider mode (legacy).
      4. Else → auto-build a chain from every provider that has a key, in the
         default order [gemini, openai, anthropic].

    Single-provider mode still wraps the client in a (single-element) chain so
    the same retry+cooldown logic applies, except that there's nothing to fail
    over to.
    """
    if provider:
        client = _build_single_client(provider.lower().strip(), rate_limit_rpm)
        if client is None:
            raise RuntimeError(
                f"Provider '{provider}' was requested but its API key is missing."
            )
        return client

    providers_env = os.getenv("LLM_PROVIDERS", "").strip()
    legacy_provider = os.getenv("LLM_PROVIDER", "").strip().lower()

    if providers_env:
        chain_order = [p.strip().lower() for p in providers_env.split(",") if p.strip()]
    elif legacy_provider:
        chain_order = [legacy_provider]
    else:
        chain_order = ["gemini", "openai", "anthropic"]

    clients: List[LLMClient] = []
    skipped: List[str] = []
    for prov in chain_order:
        client = _build_single_client(prov, rate_limit_rpm)
        if client is not None:
            clients.append(client)
        else:
            skipped.append(prov)

    if skipped:
        logger.info(
            "Skipped providers (no API key): %s. Active chain: %s",
            ", ".join(skipped),
            ", ".join(c.provider_name for c in clients) or "(none)",
        )

    if not clients:
        raise RuntimeError(
            "No LLM provider available. Set at least one of "
            "GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY in .env."
        )

    if len(clients) == 1:
        # Single provider — return it raw so its provider_name/model are stable
        # and we don't pay the trivial overhead of the chain wrapper.
        logger.info("LLM mode: single-provider (%s).", clients[0].provider_name)
        return clients[0]

    chain = FallbackLLMClient(clients)
    logger.info("LLM mode: cascade fallback. Chain: %s", chain.chain_description)
    return chain


class AnalysisFailure(Exception):
    """Raised when the LLM response cannot be coerced into the schema after retries."""


async def analyze_thread_safely(
    client: LLMClient, thread: ThreadInput
) -> Tuple[Optional[Analysis], TokenUsage, str]:
    """Wrap analyze() to never raise EXCEPT for AllProvidersExhausted.

    Returns (analysis_or_None, usage, status) where status ∈ {"ok",
    "failed_validation", "failed_llm"}. The single exception we let propagate
    is `AllProvidersExhausted`, because it's an actionable signal for the
    batch script: every provider is dead, abort the run and don't waste cycles
    on the remaining workers.
    """
    try:
        analysis, usage = await client.analyze(thread)
        return analysis, usage, "ok"
    except AllProvidersExhausted:
        # Re-raise so the batch worker can detect chain exhaustion and abort.
        raise
    except ValidationError as e:
        logger.warning("Validation failed for case=%s: %s", thread.case_id, e)
        return None, TokenUsage(model=client.model, provider=client.provider_name), "failed_validation"
    except Exception as e:
        logger.exception("LLM call failed for case=%s: %s", thread.case_id, e)
        return None, TokenUsage(model=client.model, provider=client.provider_name), "failed_llm"
