from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError
import os
import random
from email.utils import parsedate_to_datetime
import tiktoken
from typing import Callable, Optional, TypeVar
from celery.exceptions import SoftTimeLimitExceeded
from pydantic import BaseModel
import re
from time import sleep
from datetime import datetime, timezone
import logging
from backend.util.task_analytics import record_scoped_llm_response
client = None
from pathlib import Path
SYSTEM_PROMPT = (Path(__file__).parent/'prompts'/'SYSTEM').read_text()
GPT_6_1_SOL_MODEL = 'gpt-6.1-sol'
DEFAULT_MODEL = GPT_6_1_SOL_MODEL
GPT_6_SOL_MODEL = 'gpt-6-sol'
MAX_RETRIES = 50
CHAT_COMPLETION_MAX_ATTEMPTS = 4
CHAT_COMPLETION_MAX_RETRY_DELAY_SECONDS = 60
_OPENAI_RETRY_HINT_PATTERN = re.compile('Please try again in (?:(\\d+(?:\\.\\d+)?)ms|(\\d+(?:\\.\\d+)?)s)')
_ResponseValue = TypeVar('_ResponseValue')
_ParsedModel = TypeVar('_ParsedModel', bound=BaseModel)
_CHAT_COMPLETION_NON_RETRYABLE_RATE_LIMIT_CODES = {'billing_hard_limit_reached', 'billing_not_active', 'credit_balance_exhausted', 'insufficient_quota', 'organization_spend_limit_exceeded', 'organization_usage_limit_exceeded', 'project_spend_limit_exceeded'}
_CHAT_COMPLETION_TRANSIENT_RESPONSE_CODES = {'rate_limit_exceeded', 'server_error', 'server_is_overloaded', 'slow_down'}
logger = logging.getLogger(__name__)

def get_openai_client():
    """Create the client lazily and recheck scoped worker account/workflow access.

    The agent worker enters service.execution before nested drafting calls.
    That context lets this shared provider adapter recheck SQL cancellation and
    worker ownership without each drafting helper accepting workflow arguments.
    End the validation transaction before provider I/O. with_options disables
    network timeouts only for this execution; the cached default client used
    by other callers is unchanged. Celery bounds the complete task, including
    all its provider calls. This check does not count planning operations;
    before_call and save_classification in workspace_service handle that
    separately.
    """
    global client
    from backend.util.account_access import check_current_account
    check_current_account()
    if client is None:
        client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'), max_retries=0)
    from backend.util.workspace_service import current_execution, validate_execution
    if current_execution():
        from backend.database import db
        validate_execution()
        db.session.commit()
        return client.with_options(timeout=None)
    return client

def _extract_openai_retry_hint(error_message: str) -> tuple[int | None, str | None]:
    match = _OPENAI_RETRY_HINT_PATTERN.search(error_message)
    if match is None:
        return (None, None)
    if match.group(1):
        seconds = float(match.group(1)) / 1000.0
        retry_after = 1 if seconds < 1 else int(seconds + 1)
    else:
        retry_after = int(float(match.group(2)) + 1)
    return (retry_after, match.group(0))

def _value_from_object(value, key: str, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)

def _chat_completion_error_code(error) -> str | None:
    direct_code = getattr(error, 'code', None)
    if direct_code:
        return str(direct_code)
    body = getattr(error, 'body', None)
    if isinstance(body, dict):
        nested_error = body.get('error', body)
        if isinstance(nested_error, dict):
            code = nested_error.get('code') or nested_error.get('type')
            if code:
                return str(code)
    return None

def _chat_completion_response_error_code(response) -> str | None:
    response_error = _value_from_object(response, 'error')
    code = _value_from_object(response_error, 'code')
    return str(code) if code else None

def _chat_completion_has_refusal(response) -> bool:
    for output_item in _value_from_object(response, 'output', []) or []:
        if _value_from_object(output_item, 'type') == 'refusal':
            return True
        for content_item in _value_from_object(output_item, 'content', []) or []:
            if _value_from_object(content_item, 'type') == 'refusal':
                return True
    return False

def _chat_completion_retry_hint(error) -> tuple[float | None, str, str | None]:
    response = getattr(error, 'response', None)
    headers = getattr(response, 'headers', None)
    if headers:
        retry_after_ms = headers.get('retry-after-ms')
        if retry_after_ms is not None:
            try:
                return (max(0.0, float(retry_after_ms) / 1000.0), 'retry-after-ms', None)
            except (TypeError, ValueError):
                pass
        retry_after = headers.get('retry-after')
        if retry_after is not None:
            try:
                return (max(0.0, float(retry_after)), 'retry-after', None)
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(str(retry_after))
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    seconds = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                    return (seconds, 'retry-after', None)
                except (TypeError, ValueError, OverflowError):
                    pass
    message = getattr(error, 'message', None) or str(error)
    (parsed_delay, matched_wait) = _extract_openai_retry_hint(message)
    if parsed_delay is not None:
        return (float(parsed_delay), 'message', matched_wait)
    return (None, 'fallback', None)

def _chat_completion_backoff_seconds(retry_number: int) -> float:
    base_delay = min(2 ** max(0, retry_number - 1), 10)
    return base_delay + random.uniform(0.0, 0.25)

def _chat_completion_503_backoff_seconds(retry_number: int) -> float:
    return min(5 * 2 ** max(0, retry_number - 1), 20) + random.uniform(0.0, 1.0)

def _chat_completion_status_retry_delay(error: APIStatusError, attempt: int, *, operation: str, model: str) -> float:
    (retry_delay, hint_source, matched_wait) = _chat_completion_retry_hint(error)
    parsed_hint_seconds = retry_delay
    if retry_delay is None:
        if getattr(error, 'status_code', None) == 503:
            retry_delay = _chat_completion_503_backoff_seconds(attempt)
        else:
            retry_delay = _chat_completion_backoff_seconds(attempt)
    if getattr(error, 'status_code', None) == 503:
        matched_wait_log = ' matched_wait={0!r}'.format(matched_wait) if matched_wait else ''
        logger.warning('%s http_503 model=%s attempt=%s status_code=503 hint_source=%s parsed_hint_seconds=%s delay=%s%s', operation, model, attempt, hint_source, parsed_hint_seconds, retry_delay, matched_wait_log)
    return retry_delay

def _chat_completion_should_retry_status_error(error: APIStatusError) -> bool:
    status_code = getattr(error, 'status_code', None)
    if status_code == 429:
        error_code = _chat_completion_error_code(error)
        return error_code not in _CHAT_COMPLETION_NON_RETRYABLE_RATE_LIMIT_CODES
    return status_code in {408, 409} or (isinstance(status_code, int) and status_code >= 500)

def _chat_completion_response_total_tokens(response) -> int:
    usage = _value_from_object(response, 'usage')
    value = _value_from_object(usage, 'total_tokens', 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

def _effective_reasoning_effort(model: str, reasoning_effort: str | None) -> str | None:
    """Pin Sol's medium effort while preserving explicit caller overrides."""
    if reasoning_effort is not None:
        return reasoning_effort
    return 'medium' if model in {GPT_6_SOL_MODEL, GPT_6_1_SOL_MODEL} else None

def _responses_with_retries(*, model: str, operation: str, request: Callable[[], object], extract: Callable[[object], _ResponseValue | None], token_counter=None, raise_on_incomplete=False) -> _ResponseValue | None:
    """Run one Responses request with bounded retries and per-attempt accounting."""
    from backend.util.workspace_service import WorkflowStopped
    from backend.util.attachment_parser import WorkPending
    for attempt in range(1, CHAT_COMPLETION_MAX_ATTEMPTS + 1):
        retry_delay = None
        response_id = None
        status = None
        try:
            response = request()
        except APIConnectionError as error:
            failure_class = type(error).__name__
            retry_delay = _chat_completion_backoff_seconds(attempt)
        except APIStatusError as error:
            failure_class = 'http_{0}'.format(getattr(error, 'status_code', 'unknown'))
            if not _chat_completion_should_retry_status_error(error):
                logger.warning('%s terminal model=%s attempt=%s failure_class=%s code=%s', operation, model, attempt, failure_class, _chat_completion_error_code(error))
                return None
            retry_delay = _chat_completion_status_retry_delay(error, attempt, operation=operation, model=model)
        except OpenAIError as error:
            if raise_on_incomplete and 'LengthFinishReason' in type(error).__name__:
                from backend.ai.token_budget import IncompleteStructuredOutput
                raise IncompleteStructuredOutput('Structured provider output was incomplete') from error
            logger.warning('%s terminal model=%s attempt=%s failure_class=%s', operation, model, attempt, type(error).__name__)
            return None
        except (SoftTimeLimitExceeded, WorkflowStopped, WorkPending):
            raise
        except Exception as error:
            logger.warning('%s terminal model=%s attempt=%s failure_class=%s', operation, model, attempt, type(error).__name__)
            return None
        else:
            record_scoped_llm_response(response, model=model)
            if token_counter is not None:
                token_counter['total'] = token_counter.get('total', 0) + _chat_completion_response_total_tokens(response)
            response_id = _value_from_object(response, 'id')
            status = _value_from_object(response, 'status') or 'completed'
            if _chat_completion_has_refusal(response):
                failure_class = 'refusal'
            elif status == 'incomplete':
                if raise_on_incomplete:
                    from backend.ai.token_budget import IncompleteStructuredOutput
                    raise IncompleteStructuredOutput('Structured provider output was incomplete')
                reason = _value_from_object(_value_from_object(response, 'incomplete_details'), 'reason', 'unknown')
                failure_class = 'incomplete:{0}'.format(reason)
            elif status == 'failed':
                code = _chat_completion_response_error_code(response)
                failure_class = 'failed:{0}'.format(code or 'unknown')
                if code in _CHAT_COMPLETION_TRANSIENT_RESPONSE_CODES:
                    retry_delay = _chat_completion_backoff_seconds(attempt)
                else:
                    logger.warning('%s terminal model=%s attempt=%s response_id=%s status=%s failure_class=%s', operation, model, attempt, response_id, status, failure_class)
                    return None
            elif status != 'completed':
                failure_class = 'status:{0}'.format(status)
            else:
                try:
                    output = extract(response)
                except SoftTimeLimitExceeded:
                    raise
                except Exception as error:
                    logger.warning('%s terminal model=%s attempt=%s response_id=%s status=%s failure_class=%s', operation, model, attempt, response_id, status, type(error).__name__)
                    return None
                if output is not None and (not isinstance(output, str) or output.strip()):
                    return output
                failure_class = 'empty_output'
                retry_delay = _chat_completion_backoff_seconds(attempt)
            if retry_delay is None:
                logger.warning('%s terminal model=%s attempt=%s response_id=%s status=%s failure_class=%s', operation, model, attempt, response_id, status, failure_class)
                return None
        if attempt == CHAT_COMPLETION_MAX_ATTEMPTS:
            break
        if retry_delay > CHAT_COMPLETION_MAX_RETRY_DELAY_SECONDS:
            logger.warning('%s terminal model=%s attempt=%s response_id=%s status=%s failure_class=retry_delay_too_long delay=%s', operation, model, attempt, response_id, status, retry_delay)
            return None
        logger.warning('%s retry model=%s attempt=%s response_id=%s status=%s failure_class=%s delay=%s', operation, model, attempt, response_id, status, failure_class, retry_delay)
        sleep(retry_delay)
    logger.warning('%s attempts_exhausted model=%s attempts=%s', operation, model, CHAT_COMPLETION_MAX_ATTEMPTS)
    return None

def chat_completion(prompt: str, model: str=DEFAULT_MODEL, *, sys_prompt: str=SYSTEM_PROMPT, token_counter=None, reasoning_effort: str | None=None) -> str | None:
    """Return a free-text Responses API result, or ``None`` on terminal failure."""
    request_kwargs = {'model': model, 'input': [{'role': 'system', 'content': sys_prompt}, {'role': 'user', 'content': prompt}]}
    effective_effort = _effective_reasoning_effort(model, reasoning_effort)
    if effective_effort is not None:
        request_kwargs['reasoning'] = {'effort': effective_effort}
    response = responses_completion(request_kwargs, token_counter=token_counter)
    return _value_from_object(response, 'output_text') if response is not None else None

def responses_completion(request_kwargs, *, token_counter=None):
    """Send prepared context and available tool definitions to the LLM.

    Return the complete model response so the controller can read its requested
    tool calls, using the existing shared retry and usage-accounting behavior.
    """
    from backend.util.workspace_service import WorkflowStopped
    from backend.util.attachment_parser import WorkPending
    from backend.ai.token_budget import validate_text_request
    validate_text_request(request_kwargs)
    model = request_kwargs['model']
    for attempt in range(1, CHAT_COMPLETION_MAX_ATTEMPTS + 1):
        retry_delay = None
        try:
            logger.info('chat_completion attempt=%s/%s model=%s', attempt, CHAT_COMPLETION_MAX_ATTEMPTS, model)
            response = get_openai_client().responses.create(**request_kwargs)
            record_scoped_llm_response(response, model=model)
            total_tokens = _chat_completion_response_total_tokens(response)
            if token_counter is not None:
                token_counter['total'] = token_counter.get('total', 0) + total_tokens
            response_id = _value_from_object(response, 'id')
            status = _value_from_object(response, 'status') or 'completed'
            if _chat_completion_has_refusal(response):
                logger.warning('chat_completion refusal model=%s attempt=%s response_id=%s status=%s', model, attempt, response_id, status)
                return None
            if status == 'incomplete':
                incomplete_details = _value_from_object(response, 'incomplete_details')
                incomplete_reason = _value_from_object(incomplete_details, 'reason', 'unknown')
                logger.warning('chat_completion incomplete model=%s attempt=%s response_id=%s reason=%s', model, attempt, response_id, incomplete_reason)
                return None
            if status == 'failed':
                response_error_code = _chat_completion_response_error_code(response)
                if response_error_code in _CHAT_COMPLETION_TRANSIENT_RESPONSE_CODES:
                    logger.warning('chat_completion transient_response model=%s attempt=%s response_id=%s code=%s', model, attempt, response_id, response_error_code)
                    retry_delay = _chat_completion_backoff_seconds(attempt)
                else:
                    logger.warning('chat_completion terminal_response model=%s attempt=%s response_id=%s status=%s code=%s', model, attempt, response_id, status, response_error_code)
                    return None
            elif status in {'cancelled', 'queued', 'in_progress'}:
                logger.warning('chat_completion unusable_response model=%s attempt=%s response_id=%s status=%s', model, attempt, response_id, status)
                return None
            elif status != 'completed':
                logger.warning('chat_completion unknown_response_status model=%s attempt=%s response_id=%s status=%s', model, attempt, response_id, status)
                return None
            else:
                output_text = _value_from_object(response, 'output_text')
                if output_text is not None and str(output_text).strip() or any((_value_from_object(item, 'type') == 'function_call' for item in _value_from_object(response, 'output', []) or [])):
                    return response
                logger.warning('chat_completion empty_response model=%s attempt=%s response_id=%s status=%s', model, attempt, response_id, status)
                retry_delay = _chat_completion_backoff_seconds(attempt)
        except APIConnectionError as error:
            logger.warning('chat_completion retryable_exception model=%s attempt=%s error_type=%s', model, attempt, type(error).__name__)
            retry_delay = _chat_completion_backoff_seconds(attempt)
        except APIStatusError as error:
            status_code = getattr(error, 'status_code', None)
            error_code = _chat_completion_error_code(error)
            if not _chat_completion_should_retry_status_error(error):
                logger.warning('chat_completion terminal_api_error model=%s attempt=%s status_code=%s code=%s error_type=%s', model, attempt, status_code, error_code, type(error).__name__)
                return None
            retry_delay = _chat_completion_status_retry_delay(error, attempt, operation='chat_completion', model=model)
            logger.warning('chat_completion retryable_api_error model=%s attempt=%s status_code=%s code=%s error_type=%s', model, attempt, status_code, error_code, type(error).__name__)
        except OpenAIError as error:
            logger.warning('chat_completion terminal_openai_error model=%s attempt=%s error_type=%s', model, attempt, type(error).__name__)
            return None
        except (SoftTimeLimitExceeded, WorkflowStopped, WorkPending):
            raise
        except Exception as error:
            logger.warning('chat_completion unexpected_error model=%s attempt=%s error_type=%s', model, attempt, type(error).__name__)
            return None
        if attempt == CHAT_COMPLETION_MAX_ATTEMPTS:
            break
        if retry_delay > CHAT_COMPLETION_MAX_RETRY_DELAY_SECONDS:
            logger.warning('chat_completion retry_delay_too_long model=%s attempt=%s delay=%s', model, attempt, retry_delay)
            return None
        logger.warning('chat_completion retrying model=%s next_attempt=%s delay=%s', model, attempt + 1, retry_delay)
        sleep(retry_delay)
    logger.warning('chat_completion attempts_exhausted model=%s attempts=%s', model, CHAT_COMPLETION_MAX_ATTEMPTS)
    return None

def chat_completion_j(prompt: str, model=DEFAULT_MODEL, sys_prompt=SYSTEM_PROMPT, token_counter=None, reasoning_effort: str | None=None) -> str | None:
    """Deprecated raw-JSON compatibility path for callers without typed schemas."""
    request_kwargs = {'model': model, 'input': [{'role': 'system', 'content': sys_prompt}, {'role': 'user', 'content': prompt}], 'text': {'format': {'type': 'json_object'}}}
    effective_effort = _effective_reasoning_effort(model, reasoning_effort)
    if effective_effort is not None:
        request_kwargs['reasoning'] = {'effort': effective_effort}
    from backend.ai.token_budget import validate_text_request
    validate_text_request(request_kwargs)
    return _responses_with_retries(model=model, operation='chat_completion_j', request=lambda : get_openai_client().responses.create(**request_kwargs), extract=lambda response: _value_from_object(response, 'output_text'), token_counter=token_counter)

def chat_completion_parse(prompt: str, response_model: type[_ParsedModel], *, model: str, reasoning_effort: str | None, sys_prompt: str=SYSTEM_PROMPT, token_counter=None, max_output_tokens: int | None=None, raise_on_incomplete: bool=False) -> _ParsedModel | None:
    """Return a validated Pydantic result from a single-turn Responses request."""
    request_kwargs = {'model': model, 'input': [{'role': 'system', 'content': sys_prompt}, {'role': 'user', 'content': prompt}], 'text_format': response_model}
    if max_output_tokens is not None:
        request_kwargs['max_output_tokens'] = max_output_tokens
    effective_effort = _effective_reasoning_effort(model, reasoning_effort)
    if effective_effort is not None:
        request_kwargs['reasoning'] = {'effort': effective_effort}
    from backend.ai.token_budget import validate_text_request
    from backend.ai.parse_diagnostics import parse_response
    validate_text_request(request_kwargs)
    return _responses_with_retries(model=model, operation='chat_completion_parse', request=lambda : parse_response(get_openai_client(), request_kwargs), extract=lambda response: _value_from_object(response, 'output_parsed'), token_counter=token_counter, raise_on_incomplete=raise_on_incomplete)

def web_search_completion(prompt: str, model: str=DEFAULT_MODEL, allowed_domains: Optional[list[str]]=None, return_meta: bool=False, reasoning_effort: str | None=None, *, sys_prompt: str | None=None, prev_prompt_output: list[tuple[str, str]] | None=None):
    """Run a web-enabled response and return the text.

    Optionally returns best-effort source/annotation metadata for callers that
    need to verify exact matches.
    """
    tool = {'type': 'web_search'}
    if allowed_domains:
        tool['filters'] = {'allowed_domains': allowed_domains[:100]}
    messages = []
    if sys_prompt is not None:
        messages.append({'role': 'system', 'content': sys_prompt})
    for (previous_prompt, previous_output) in prev_prompt_output or []:
        messages.append({'role': 'user', 'content': previous_prompt})
        messages.append({'role': 'assistant', 'content': previous_output})
    messages.append({'role': 'user', 'content': prompt})
    request_kwargs = {'model': model, 'input': messages, 'tools': [tool], 'include': ['web_search_call.action.sources']}
    effective_effort = _effective_reasoning_effort(model, reasoning_effort)
    if effective_effort is not None:
        request_kwargs['reasoning'] = {'effort': effective_effort}
    for attempt in range(1, CHAT_COMPLETION_MAX_ATTEMPTS + 1):
        try:
            resp = get_openai_client().responses.create(**request_kwargs)
        except APIConnectionError as error:
            last_error = error
            retry_delay = _chat_completion_backoff_seconds(attempt)
        except APIStatusError as error:
            if not _chat_completion_should_retry_status_error(error):
                raise
            last_error = error
            retry_delay = _chat_completion_status_retry_delay(error, attempt, operation='web_search_completion', model=model)
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            raise
        else:
            record_scoped_llm_response(resp, model=model)
            break
        if attempt == CHAT_COMPLETION_MAX_ATTEMPTS or retry_delay > CHAT_COMPLETION_MAX_RETRY_DELAY_SECONDS:
            raise last_error
        logger.warning('web_search_completion retry model=%s attempt=%s failure_class=%s delay=%s', model, attempt, type(last_error).__name__, retry_delay)
        sleep(retry_delay)
    output_text = _value_from_object(resp, 'output_text', '') or ''
    if not return_meta:
        return output_text
    sources = []
    annotations = []
    try:
        out_items = _value_from_object(resp, 'output') or []
        for item in out_items:
            item_type = _value_from_object(item, 'type')
            if item_type == 'web_search_call':
                action = _value_from_object(item, 'action')
                sources.extend(_value_from_object(action, 'sources') or [])
            elif item_type == 'message':
                for content_item in _value_from_object(item, 'content') or []:
                    if _value_from_object(content_item, 'type') == 'output_text':
                        annotations.extend(_value_from_object(content_item, 'annotations') or [])
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        pass
    return (output_text, {'sources': sources if sources else _value_from_object(resp, 'sources'), 'annotations': annotations})

def num_tokens_from_string(string: str, encoding_name='cl100k_base') -> int:
    """Returns the number of tokens in a text string."""
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens

def truncate_text_to_tokens(text: str, max_tokens: int, encoding_name='cl100k_base') -> str:
    """Truncate text to at most max_tokens using tiktoken."""
    if not text or max_tokens <= 0:
        return ''
    encoding = tiktoken.get_encoding(encoding_name)
    tokens = encoding.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return encoding.decode(tokens[:max_tokens]) + '...'
