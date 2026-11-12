"""Bounded JSON calls for interactive Word tasks, using LexText's AI client."""
import json
import logging
import re
import time
import uuid

logger = logging.getLogger(__name__)
JSON_INSTRUCTION = "\nReturn exactly one valid JSON object matching the requested response contract. No Markdown fences."


def _safe(value):
    # Only provider identifiers/codes, never exception text or document content.
    value = str(value or '')
    return value if re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}', value) else 'unknown'


def _code(error):
    body = getattr(error, 'body', None)
    if isinstance(body, dict):
        nested = body.get('error', body)
        if isinstance(nested, dict):
            return _safe(nested.get('code'))
    return _safe(getattr(error, 'code', None))


def _retry_delay(error):
    headers = getattr(getattr(error, 'response', None), 'headers', {}) or {}
    try:
        delay = float(headers.get('retry-after', 2))
        # A long quota wait should return to the lawyer instead of tying up a worker.
        return max(1, delay) if 0 <= delay <= 10 else None
    except (ValueError, TypeError):
        return 2


def _message(status, code, kind):
    if status in (401, 403) or code in {'insufficient_quota', 'billing_hard_limit_reached'}:
        return "LexText's analysis service needs an administrator to check its access or billing configuration. Retrying this document will not fix it."
    if status == 429:
        return "LexText's analysis service is busy or rate-limited. Wait a moment, then retry."
    if status is not None and status >= 500:
        return "LexText's analysis provider is temporarily unavailable. Please retry shortly."
    if status is not None and status >= 400:
        return "The analysis provider rejected this request. Share the reference below with LexText support so its request configuration can be checked."
    if 'Timeout' in kind:
        return "The analysis service took too long to respond. Retry, or try a smaller section."
    return "LexText could not connect to the analysis service. Please retry shortly."


def complete_json(prompt, system, *, interactive=False):
    from backend.ai.base import get_openai_client, DEFAULT_MODEL
    from openai import APIConnectionError, APIStatusError

    # A short rewrite must not silently spend four minutes retrying a slow model.
    # Keep LexText's model and quality safeguards; lower reasoning only for the
    # interactive passage profile on supported GPT-5 models, never Pro/unknown models.
    options = {}
    if interactive and re.fullmatch(r'gpt-5(?:\.\d+)?(?:-\d{4}-\d{2}-\d{2})?', DEFAULT_MODEL):
        options['reasoning'] = {'effort': 'low'}
    started = time.monotonic()
    deadline = started + (90 if interactive else 250)
    timeout = 60.0 if interactive else 120.0
    client = get_openai_client().with_options(timeout=timeout, max_retries=0)
    reference = uuid.uuid4().hex[:12]
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(f"The analysis service took too long to respond. Your document is unchanged. Please retry. Reference: {reference}.")
        attempt_client = client if remaining >= timeout else client.with_options(timeout=remaining, max_retries=0)
        try:
            response = attempt_client.responses.create(
                model=DEFAULT_MODEL,
                input=[{"role": "system", "content": system + JSON_INSTRUCTION},
                       {"role": "user", "content": prompt}],
                text={"format": {"type": "json_object"}},
                **options,
            )
        except (APIConnectionError, APIStatusError) as error:
            status = getattr(error, 'status_code', None)
            code = _code(error)
            kind = type(error).__name__
            logger.warning(
                'Word model failure: reference=%s type=%s status=%s code=%s request_id=%s attempt=%s',
                reference, _safe(kind), status, code,
                _safe(getattr(error, 'request_id', None)), attempt + 1,
            )
            retryable = (status is None or status == 429 or status >= 500) and code not in {
                'insufficient_quota', 'billing_hard_limit_reached',
            }
            delay = _retry_delay(error)
            if (attempt == 0 and retryable and delay is not None
                    and time.monotonic() + delay < deadline
                    and not (interactive and 'Timeout' in kind)):
                time.sleep(delay)
                continue
            raise ValueError(f"{_message(status, code, kind)} Your document is unchanged. Reference: {reference}.") from None
        if getattr(response, 'status', 'completed') == 'completed':
            try:
                value = json.loads(response.output_text)
                if isinstance(value, dict):
                    logger.info('Word model complete: reference=%s profile=%s elapsed_seconds=%.1f attempts=%s',
                                reference, 'passage' if interactive else 'standard', time.monotonic() - started, attempt + 1)
                    return value
            except (ValueError, TypeError):
                pass
        logger.warning('Word model returned incomplete JSON: reference=%s attempt=%s', reference, attempt + 1)
    raise ValueError(f"LexText could not complete the analysis after retrying. Nothing was changed in Word. Try a smaller section. Reference: {reference}.")
