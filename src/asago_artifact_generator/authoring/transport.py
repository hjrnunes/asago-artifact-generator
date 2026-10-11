"""The OpenAI-compatible provider client used for private-model authoring."""

from __future__ import annotations

import time
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from ..value_checks import is_nonblank_str
from .context_budget import _context_budget_estimate, _enforce_context_budget
from .core import (
    _CONTEXT_FRAMING_TOKEN_RESERVE,
    _REVIEW_STAGES,
    AUTHORING_MAX_COMPLETION_TOKENS,
    PromptPacket,
    PromptPreflightError,
    TransportResponse,
    _model_dump,
)
from .prompt_safety import _endpoint_identity, _endpoint_prompt_paths
from .response_decode import _provider_field, _provider_response_capture

RETRY_DELAY_SECONDS = 1.0
# Why a physical request was sent.  The ledger keeps the first attempt as the
# dispatch record itself and lists later attempts in ``transport_retries``,
# where only the service-tier fallback spells out its reason.
FIRST_ATTEMPT = "first_attempt"
TRANSPORT_RETRY = "transport_retry"
SERVICE_TIER_FALLBACK_REASON = "service_tier_fallback"
_sleep = time.sleep


class AttemptRefused(Exception):
    """The request budget refused one more physical request."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"request budget refused the {reason} request")
        self.reason = reason


class RequestBoundary:
    """The one place a physical model request is sent.

    :meth:`dispatch` asks ``gate`` to reserve the request, records the
    attempt with its reason, and sends exactly one request through ``send``.
    Retry and fallback rules decide whether to ask for another dispatch; they
    never send.  ``gate`` returns False to refuse; it may also raise, and the
    exception surfaces unchanged.  Without a gate every attempt is allowed.
    """

    def __init__(
        self, send: Callable[..., Any], gate: Callable[[str], bool] | None = None
    ) -> None:
        self._send = send
        self._gate = gate
        self.attempts: list[dict[str, Any]] = []

    def dispatch(
        self, request: dict[str, Any], reason: str, retry_of: dict[str, Any] | None = None
    ) -> Any:
        if self._gate is not None and not self._gate(reason):
            raise AttemptRefused(reason)
        self.attempts.append(
            {"attempt": len(self.attempts) + 1, "reason": reason, "retry_of": retry_of}
        )
        if reason == TRANSPORT_RETRY:
            _sleep(RETRY_DELAY_SECONDS)
        return self._send(**request)

    def extra_requests(self) -> list[dict[str, Any]]:
        """Return the attempts after the first as the ledger's ``transport_retries``."""

        return [_ledger_retry(attempt) for attempt in self.attempts[1:]]


def _ledger_retry(attempt: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {"attempt": attempt["attempt"], "retry_of": attempt["retry_of"]}
    if attempt["reason"] != TRANSPORT_RETRY:
        record["reason"] = attempt["reason"]
    return record


def retryable_failure(error: BaseException) -> dict[str, Any] | None:
    """Describe *error* as ``{"type", "status_code"}`` when it earns the one retry.

    A request retries after an HTTP 5xx status or a connection error that is
    not a timeout.  A timeout, every 4xx status (429 included), and every
    other error never retries.
    """

    import openai

    if isinstance(error, openai.APITimeoutError):
        return None
    if isinstance(error, openai.APIConnectionError):
        return {"type": type(error).__name__, "status_code": None}
    if isinstance(error, openai.APIStatusError) and 500 <= error.status_code <= 599:
        return {"type": type(error).__name__, "status_code": error.status_code}
    return None


class PrivateModelAuthoringTransport:
    """Explicit OpenAI-compatible private authoring client.

    The SDK's own retries are off (``max_retries = 0``).  Every physical
    request goes through one :class:`RequestBoundary`: the first attempt, the
    one retry after a transport error (see :func:`retryable_failure`), and the
    service-tier fallback after a rate limit.  ``attempt_gate`` receives the
    reason of each request and lets the caller reserve or refuse it, for
    example when the request budget has no request left.  ``last_retries``
    lists the requests after the first in the ledger's shape.
    """

    max_retries = 0

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        profile_name: str | None = None,
        temperature: float = 0.0,
        extra_body: dict[str, Any] | None = None,
        review_extra_body: dict[str, Any] | None = None,
        max_completion_tokens: int | None = None,
        context_window_tokens: int | None = None,
        review_fill_context: bool = False,
        reasoning_effort: str | None = None,
        service_tier: str | None = None,
        service_tier_fallback: str | None = None,
        sampling_controls: bool = True,
        strict_json_schema: bool | None = None,
        timeout: float | int | None = None,
        client: Any | None = None,
    ) -> None:
        """Create the client.

        ``client`` replaces the ``openai.OpenAI`` client that the endpoint
        options would build; replay passes one that serves recorded responses.
        ``extra_body`` applies to author and correction requests.  When
        ``review_extra_body`` is supplied it replaces ``extra_body`` for
        semantic-review requests; otherwise reviews use ``extra_body`` too.

        With ``review_fill_context``, a semantic-review request's completion
        limit is the context window minus the conservative prompt estimate and
        the framing reserve.  The context guard still reserves
        ``max_completion_tokens``, so a review that passes the guard never
        receives less than that limit.
        """

        from openai import OpenAI

        _validate_transport_options(
            max_completion_tokens=max_completion_tokens,
            context_window_tokens=context_window_tokens,
            review_fill_context=review_fill_context,
        )
        self.model = model
        self.profile_name = profile_name
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.service_tier = service_tier
        self.service_tier_fallback = service_tier_fallback
        self.sampling_controls = sampling_controls
        self.strict_json_schema = strict_json_schema
        self.timeout = timeout
        self.last_controls: dict[str, Any] | None = None
        self.attempt_gate: Callable[[str], bool] | None = None
        self.extra_body = deepcopy(extra_body) if extra_body is not None else None
        self.review_extra_body = (
            deepcopy(review_extra_body) if review_extra_body is not None else None
        )
        self.max_completion_tokens = max_completion_tokens
        self.context_window_tokens = context_window_tokens
        self.review_fill_context = review_fill_context
        self._endpoint_netloc, self._endpoint_hostname = _endpoint_identity(base_url)
        client_options: dict[str, Any] = {
            "base_url": base_url,
            "api_key": api_key,
            "max_retries": 0,
        }
        if timeout is not None:
            client_options["timeout"] = timeout
        self._client = client if client is not None else OpenAI(**client_options)
        self._boundary = self._new_boundary()

    def extra_body_for(self, packet: PromptPacket) -> dict[str, Any] | None:
        """Return the extra_body controls for this packet's role."""

        body = (
            self.review_extra_body
            if packet.stage in _REVIEW_STAGES and self.review_extra_body is not None
            else self.extra_body
        )
        if body is None:
            return None
        result = deepcopy(body)
        if not self.sampling_controls:
            for key in ("chat_template_kwargs", "temperature", "top_p", "top_k", "seed"):
                result.pop(key, None)
        return result or None

    def max_completion_tokens_for(self, packet: PromptPacket) -> int | None:
        """Return the completion limit sent with this packet."""

        if (
            self.review_fill_context
            and packet.stage in _REVIEW_STAGES
            and self.context_window_tokens is not None
            and self.max_completion_tokens is not None
        ):
            estimate = _context_budget_estimate(packet)["estimated_prompt_tokens"]
            filled = self.context_window_tokens - int(estimate) - _CONTEXT_FRAMING_TOKEN_RESERVE
            # The default completion limit is a floor that a review fills from
            # the remaining context; treat a larger profile completion limit
            # as the review role's explicit cap. A 1.05M context must not turn
            # a review into a million-token request.
            if self.max_completion_tokens > AUTHORING_MAX_COMPLETION_TOKENS:
                return min(filled, self.max_completion_tokens)
            return max(filled, self.max_completion_tokens)
        return self.max_completion_tokens

    def _new_boundary(self) -> RequestBoundary:
        """Return the boundary of one call: the only sender of the SDK's create request."""

        return RequestBoundary(self._client.chat.completions.create, self.attempt_gate)

    @property
    def last_retries(self) -> list[dict[str, Any]]:
        """The requests after the first of the latest call, as the ledger records them."""

        return self._boundary.extra_requests()

    def complete(self, packet: PromptPacket) -> TransportResponse:
        self._boundary = self._new_boundary()
        self.preflight_context_budget(packet)
        extra_body = self.extra_body_for(packet)
        max_completion_tokens = self.max_completion_tokens_for(packet)
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": packet.system},
                {"role": "user", "content": packet.user},
            ],
        }
        if self.sampling_controls:
            request["temperature"] = self.temperature
        if extra_body is not None:
            request["extra_body"] = deepcopy(extra_body)
        if max_completion_tokens is not None:
            request["max_completion_tokens"] = max_completion_tokens
        if self.reasoning_effort is not None:
            request["reasoning_effort"] = self.reasoning_effort
        if self.service_tier is not None:
            request["service_tier"] = self.service_tier
        response = self._create(request, extra_body, max_completion_tokens)
        choice = response.choices[0]
        message = choice.message
        provider_model = getattr(response, "model", None)
        content = _provider_field(message, "content")
        return TransportResponse(
            raw=content.encode("utf-8") if isinstance(content, str) else b"",
            usage=_model_dump(getattr(response, "usage", None)),
            controls=deepcopy(self.last_controls) if self.last_controls is not None else {},
            response_capture=_provider_response_capture(choice, message),
            provider_model=provider_model if is_nonblank_str(provider_model) else None,
        )

    def _create(
        self,
        request: dict[str, Any],
        extra_body: dict[str, Any] | None,
        max_completion_tokens: int | None,
    ) -> Any:
        """Dispatch the request; after a rate limit, ask once for the fallback tier."""

        self.last_controls = self._request_controls(
            extra_body=extra_body,
            max_completion_tokens=max_completion_tokens,
            service_tier=request.get("service_tier"),
            fallback_used=False,
        )
        try:
            return self._send(request, FIRST_ATTEMPT)
        except self._rate_limit_error_type() as rate_limit:
            if self.service_tier is None or self.service_tier_fallback is None:
                raise
            return self._fall_back(request, rate_limit, extra_body, max_completion_tokens)

    def _fall_back(
        self,
        request: dict[str, Any],
        rate_limit: BaseException,
        extra_body: dict[str, Any] | None,
        max_completion_tokens: int | None,
    ) -> Any:
        """Ask for the fallback-tier request; a refusal surfaces the rate limit."""

        sent_controls = self.last_controls
        fallback_request = deepcopy(request)
        fallback_request["service_tier"] = self.service_tier_fallback
        self.last_controls = self._request_controls(
            extra_body=extra_body,
            max_completion_tokens=max_completion_tokens,
            service_tier=fallback_request["service_tier"],
            fallback_used=True,
        )
        retry_of = {"type": type(rate_limit).__name__, "status_code": 429}
        try:
            return self._send(fallback_request, SERVICE_TIER_FALLBACK_REASON, retry_of)
        except AttemptRefused:
            self.last_controls = sent_controls
            raise rate_limit from None

    def _send(
        self, request: dict[str, Any], reason: str, retry_of: dict[str, Any] | None = None
    ) -> Any:
        """Dispatch one request; after a transport error, ask once for a retry."""

        try:
            return self._boundary.dispatch(request, reason, retry_of)
        except Exception as error:
            failure = retryable_failure(error)
            if failure is None:
                raise
            return self._retry(request, failure, error)

    def _retry(self, request: dict[str, Any], failure: dict[str, Any], error: Exception) -> Any:
        """Ask for the one transport retry; a refusal surfaces the first error."""

        try:
            return self._boundary.dispatch(request, TRANSPORT_RETRY, failure)
        except AttemptRefused:
            raise error from None

    def _request_controls(
        self,
        *,
        extra_body: dict[str, Any] | None,
        max_completion_tokens: int | None,
        service_tier: Any,
        fallback_used: bool,
    ) -> dict[str, Any]:
        """Return the non-secret controls for the request currently in flight."""

        controls: dict[str, Any] = {"max_retries": 0}
        if self.sampling_controls:
            controls.update({"temperature": self.temperature, "extra_body": extra_body})
        elif extra_body is not None:
            controls["extra_body"] = extra_body
        controls.update(
            _present_controls(
                max_completion_tokens=max_completion_tokens,
                context_window_tokens=self.context_window_tokens,
                reasoning_effort=self.reasoning_effort,
            )
        )
        if service_tier is not None:
            controls["service_tier"] = service_tier
            controls["service_tier_requested"] = self.service_tier
        if self.service_tier_fallback is not None:
            controls["service_tier_fallback"] = self.service_tier_fallback
            controls["service_tier_fallback_used"] = fallback_used
        if not self.sampling_controls:
            controls["sampling_controls"] = False
        controls.update(
            _present_controls(strict_json_schema=self.strict_json_schema, timeout=self.timeout)
        )
        return controls

    @staticmethod
    def _rate_limit_error_type() -> type[BaseException]:
        """Resolve the SDK exception lazily so offline fakes remain simple."""

        from openai import RateLimitError

        return RateLimitError

    def preflight_context_budget(self, packet: PromptPacket) -> dict[str, int] | None:
        """Expose the guard so orchestration can reject before reserving budget.

        The guard also rejects a prompt that names this transport's configured
        endpoint, whatever the provenance of the text that carries it.
        """

        paths = _endpoint_prompt_paths(packet, self._endpoint_netloc, self._endpoint_hostname)
        if paths:
            raise PromptPreflightError(f"secret-bearing authoring evidence: {', '.join(paths)}")
        if self.context_window_tokens is None or self.max_completion_tokens is None:
            return None
        return _enforce_context_budget(
            packet,
            context_window_tokens=self.context_window_tokens,
            max_completion_tokens=self.max_completion_tokens,
        )


def _present_controls(**values: Any) -> dict[str, Any]:
    return {name: value for name, value in values.items() if value is not None}


def _validate_transport_options(
    *,
    max_completion_tokens: int | None,
    context_window_tokens: int | None,
    review_fill_context: bool,
) -> None:
    """Raise ValueError when the token options contradict each other.

    The profile loader already checked each option on its own.
    """

    if (
        context_window_tokens is not None
        and max_completion_tokens is not None
        and max_completion_tokens + _CONTEXT_FRAMING_TOKEN_RESERVE >= context_window_tokens
    ):
        raise ValueError(
            "max_completion_tokens leaves no room for the prompt in the context window"
        )
    if review_fill_context and (context_window_tokens is None or max_completion_tokens is None):
        raise ValueError(
            "review_fill_context requires context_window_tokens and max_completion_tokens"
        )
