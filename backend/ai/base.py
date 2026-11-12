"""This file tests the AI functionality."""
from openai import (
    APIConnectionError,
    APIStatusError,
    AzureOpenAI,
    OpenAI,
    OpenAIError,
    RateLimitError,
)
import os
import random
from email.utils import parsedate_to_datetime
from dotenv import load_dotenv
import tiktoken
from backend.util.data_tools import extract_from_json
from backend.ai.complaint_parser import get_text_from_pdf_fitz
from backend.util.db import DiskMemory
from typing import Callable, Optional, TypeVar
from backend.constants import STATIC_FOLDER
from celery.exceptions import SoftTimeLimitExceeded
from pydantic import BaseModel, Field
import json
import re
from tenacity import retry, wait_fixed, wait_random_exponential, stop_after_attempt
import numpy as np
from time import sleep
from datetime import datetime, timezone
import logging

from backend.util.task_analytics import record_scoped_llm_response
from backend.util.analytics import (
    record_guided_llm_failure,
    record_guided_llm_response,
)


load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"), override=True)
client = None
# client = AzureOpenAI(
#     api_key=os.getenv("AZURE_OPENAI_API_KEY"),  
#     api_version="2024-07-01-preview",
#     azure_endpoint="https://lextext.openai.azure.com/",
#     max_retries = 0,   # disable internal retries (which retry after ~ 1 sec)
# )

SYSTEM_PROMPT = open('backend/ai/prompts/SYSTEM').read()
GPT_6_1_SOL_MODEL = "gpt-6.1-sol"
DEFAULT_MODEL = GPT_6_1_SOL_MODEL
GPT_6_SOL_MODEL = "gpt-6-sol"
MAX_RETRIES = 50    # for azure set to 5
CHAT_COMPLETION_MAX_ATTEMPTS = 4
CHAT_COMPLETION_MAX_RETRY_DELAY_SECONDS = 60
_OPENAI_RETRY_HINT_PATTERN = re.compile(
    r"Please try again in (?:(\d+(?:\.\d+)?)ms|(\d+(?:\.\d+)?)s)"
)
_ResponseValue = TypeVar("_ResponseValue")
_ParsedModel = TypeVar("_ParsedModel", bound=BaseModel)
_CHAT_COMPLETION_NON_RETRYABLE_RATE_LIMIT_CODES = {
    "billing_hard_limit_reached",
    "billing_not_active",
    "credit_balance_exhausted",
    "insufficient_quota",
    "organization_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
    "project_spend_limit_exceeded",
}
_CHAT_COMPLETION_TRANSIENT_RESPONSE_CODES = {
    "rate_limit_exceeded",
    "server_error",
    "server_is_overloaded",
    "slow_down",
}
# Configure logging
# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

log_path = os.path.join(os.path.dirname(__file__), "base.log")
handler = logging.FileHandler(log_path)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)


def get_openai_client():
    """Create the client lazily and recheck scoped worker account/workflow access.

    case_assistant_tasks enters service.execution before nested drafting calls.
    That context lets this shared provider adapter recheck SQL cancellation and
    worker ownership without each drafting helper accepting workflow arguments.
    End the validation transaction before provider I/O. with_options disables
    network timeouts only for this execution; the cached default client used
    by other callers is unchanged. Celery bounds the complete task, including
    all its provider calls. This check does not count planning operations;
    before_call and save_classification in case_assistant_workflow handle that
    separately.
    """
    global client
    from backend.util.account_access import check_current_account
    check_current_account()
    if client is None:
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"), max_retries=0)
    from backend.util.case_assistant_workflow import current_execution, validate_execution
    if current_execution():
        # High-reasoning responses can take longer than four minutes. These
        # prompt tasks have a 20-minute soft / 21-minute hard Celery limit;
        # SoftTimeLimitExceeded must escape provider retries for task cleanup.
        from backend.database import db
        validate_execution()
        db.session.commit()
        return client.with_options(timeout=None)
    return client


def _extract_openai_retry_hint(error_message: str) -> tuple[int | None, str | None]:
    match = _OPENAI_RETRY_HINT_PATTERN.search(error_message)
    if match is None:
        return None, None
    if match.group(1):
        seconds = float(match.group(1)) / 1000.0
        retry_after = 1 if seconds < 1 else int(seconds + 1)
    else:
        retry_after = int(float(match.group(2)) + 1)
    return retry_after, match.group(0)


def extract_openai_retry_in_seconds(error_message: str) -> int | None:
    """Extract a retry delay from an OpenAI error message in seconds."""
    retry_after, _ = _extract_openai_retry_hint(error_message)
    if retry_after is not None:
        logger.info("OpenAI Error suggest wait for seconds: %s", retry_after)
    else:
        logger.debug("No retry delay found in OpenAI error message.")
    return retry_after
    

def extract_azure_retry_in_seconds(error_message:str) -> int:
    # AZURE in second: My RateLimitError: Error code: 429 - {'error': {'code': '429', 'message': 'Requests to the ChatCompletions_Create Operation under Azure OpenAI API version 2024-07-01-preview have exceeded token rate limit of your current OpenAI S0 pricing tier. Please retry after 6 seconds. Please go here: https://aka.ms/oai/quotaincrease if you would like to further increase the default rate limit.'}}
    AZURE_RATELIMITERROR = re.compile(r'Please\sretry\safter\s(\d+)')
    match = re.search(AZURE_RATELIMITERROR, error_message)
    if match:
        retry_after = int(match.group(1))
        logger.info(f"Error suggest wait for seconds: {retry_after}")
        return retry_after
    else:
        logger.info(f"Failed to extract OpenAI Error suggest wait for seconds.")
        return None  


def generate_response():
    system_prompt = open('backend/ai/prompts/SYSTEM').read()
#     facts = """
# admit or deny allegations: {}
# """
#     facts_filled = facts.format('deny')

    # prompt = open('backend/ai/prompts/RESPONSE').read() + facts_filled
    prompt = """
Create an outline of the following complaint.  The outline should only have headings 
sub headings and paragraph numbers.  Start with the summary of complaint.
Do not include parties involved or case number.  
Here is the example output:

I. SMMARY OF COMPLAINT
1-3
II. JURISDICTION 
4-6
III. FACTUAL ALLEGATIONS  
A. Digital Envoy and its Technology  
8-10
B. Google Background 
18-24

"""
    print(prompt)

    fn_complaint = "backend/uploads/COMPLAINT-DigitalEnvoyv.Google.pdf"
    complaint_body = get_text_from_pdf(fn_complaint)

    completion = get_openai_client().chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt + complaint_body}
        ]
    )

    msg = completion.choices[0].message
    return msg.content


def small_response_drafter():
    """Using a Summary of a complaint a small section see if we can get line by line
    responses.  """

    system_prompt = open('backend/ai/prompts/SYSTEM').read()
    summary = """
Plaintiff Digital Envoy Inc. is accusing Google Inc. of wrongfully using their technology in non-permitted ways, thereby leading to an unlawful and enriching business growth in Google's favor without proper compensation to Digital Envoy. Since November 2000, both corporations were in an agreement that allowed Google to use Digital Envoy's technology, particularly the IP Intelligence technology for limited uses. However, Google increased its business scope and initiated non-search related advertising services on third-party sites using Digital Envoy's technology without necessary permission and adequate compensation.

Digital Envoy is seeking justice for Google's actions, claiming the misuse of their technology has resulted in substantial loss of income opportunities and has allowed Google to unjustly profit from its wrongdoings. The complaint includes multiple counts of misappropriation of trade secrets, federal and common law unfair competition, unjust enrichment, and breach of contract. 

To respond suitably, Digital Envoy is demanding a jury trial for the case, asking for actual damages, disgorgement of profits, recovery of monies earned, trebling of damages under 15 U.S.C. § 1125, punitive damages, attorney fees and costs and an injunction against Google to prohibit further wrongful use of their proprietary technology.
"""

    prompt = """
You will be given the a litigation complaint summary, and a few paragraphs from the complaint
Create responses for the Answer section of the complaint response.  
Respond to every paragraph of the section with: 
[defendant] admits/denies the allegation contained in Paragraph [paragraph number].  [reasons why the defendant should admit/deny] 
For every paragraph from the complaint sample there should be a paragraph of output.  Do not bundle paragraphs together.

Complaint Summary: {},

Complaint sample: {}
"""

    sample = """
8. Digital Envoy invented and developed technology ("IP Intelligence") which 
essentially delivers specific information about a visitor to  a website, including: geographic 
location down to the closest city, connection speed and, in some cases, the industry in which the 
visitor works.   
9. IP Intelligence represents a substantial breakthrough in Internet technology .  The 
Internet contains more than 4 billion IP addresses, which incorporate no geographical information.  
Furthermore, IP addresses are assigned in random order, providing no logical associations based 
on the numbers.  Digital Envoy's proprietary technolo gy essentially maps the Internet's ever 
changing topology and overlays a geographical map which allows the technology to tie an IP 
address to a geographic location.  Digital Envoy has applied for a patent covering certain aspects 
of its technology and has received favorable indication from the Patent and Trademark Office.   
10. Digital Envoy delivers the information obtained through IP Intelligence with 
industry -leading reliability and virtually instantaneous speed which is essent ial for the practical 
uses of this information.  
11. Digital Envoy's customers benefit from this information in a variety of ways, such 
as permitting the assemblage of basic market data, enabling the website to publish its content  in 
the visitor's language, enhancing security and enabling geographically targeted advertising.  
12. IP Intelligence also provides a superior manner of collecting this basic information, 
which otherwise would be obtained through the use of privacy -invading "cookies" or intrusive 
user questionnaires, either of which are more susceptible to abuse.  
"""

    prompt_filled = prompt.format(summary, sample)

    completion = get_openai_client().chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_filled}
        ]
    )

    msg = completion.choices[0].message
    return msg.content



def _value_from_object(value, key: str, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _chat_completion_error_code(error) -> str | None:
    direct_code = getattr(error, "code", None)
    if direct_code:
        return str(direct_code)

    body = getattr(error, "body", None)
    if isinstance(body, dict):
        nested_error = body.get("error", body)
        if isinstance(nested_error, dict):
            code = nested_error.get("code") or nested_error.get("type")
            if code:
                return str(code)
    return None


def _chat_completion_response_error_code(response) -> str | None:
    response_error = _value_from_object(response, "error")
    code = _value_from_object(response_error, "code")
    return str(code) if code else None


def _chat_completion_has_refusal(response) -> bool:
    for output_item in _value_from_object(response, "output", []) or []:
        if _value_from_object(output_item, "type") == "refusal":
            return True
        for content_item in _value_from_object(output_item, "content", []) or []:
            if _value_from_object(content_item, "type") == "refusal":
                return True
    return False


def _chat_completion_retry_hint(error) -> tuple[float | None, str, str | None]:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        retry_after_ms = headers.get("retry-after-ms")
        if retry_after_ms is not None:
            try:
                return max(0.0, float(retry_after_ms) / 1000.0), "retry-after-ms", None
            except (TypeError, ValueError):
                pass

        retry_after = headers.get("retry-after")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after)), "retry-after", None
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(str(retry_after))
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    seconds = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                    return seconds, "retry-after", None
                except (TypeError, ValueError, OverflowError):
                    pass

    message = getattr(error, "message", None) or str(error)
    parsed_delay, matched_wait = _extract_openai_retry_hint(message)
    if parsed_delay is not None:
        return float(parsed_delay), "message", matched_wait
    return None, "fallback", None


def _chat_completion_backoff_seconds(retry_number: int) -> float:
    base_delay = min(2 ** max(0, retry_number - 1), 10)
    return base_delay + random.uniform(0.0, 0.25)


def _chat_completion_503_backoff_seconds(retry_number: int) -> float:
    return min(5 * 2 ** max(0, retry_number - 1), 20) + random.uniform(0.0, 1.0)


def _chat_completion_status_retry_delay(
    error: APIStatusError, attempt: int, *, operation: str, model: str
) -> float:
    retry_delay, hint_source, matched_wait = _chat_completion_retry_hint(error)
    parsed_hint_seconds = retry_delay
    if retry_delay is None:
        if getattr(error, "status_code", None) == 503:
            retry_delay = _chat_completion_503_backoff_seconds(attempt)
        else:
            retry_delay = _chat_completion_backoff_seconds(attempt)
    if getattr(error, "status_code", None) == 503:
        # The matched phrase contains only fixed words, digits, and the unit.
        matched_wait_log = f" matched_wait={matched_wait!r}" if matched_wait else ""
        logger.warning(
            "%s http_503 model=%s attempt=%s status_code=503 hint_source=%s parsed_hint_seconds=%s delay=%s%s",
            operation, model, attempt, hint_source, parsed_hint_seconds,
            retry_delay, matched_wait_log,
        )
    return retry_delay


def _chat_completion_should_retry_status_error(error: APIStatusError) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code == 429:
        error_code = _chat_completion_error_code(error)
        return error_code not in _CHAT_COMPLETION_NON_RETRYABLE_RATE_LIMIT_CODES
    return status_code in {408, 409} or (
        isinstance(status_code, int) and status_code >= 500
    )


def _chat_completion_response_total_tokens(response) -> int:
    usage = _value_from_object(response, "usage")
    value = _value_from_object(usage, "total_tokens", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _effective_reasoning_effort(model: str, reasoning_effort: str | None) -> str | None:
    """Pin Sol's medium effort while preserving explicit caller overrides."""
    if reasoning_effort is not None:
        return reasoning_effort
    return "medium" if model in {GPT_6_SOL_MODEL, GPT_6_1_SOL_MODEL} else None


def _responses_with_retries(
    *,
    model: str,
    operation: str,
    request: Callable[[], object],
    extract: Callable[[object], _ResponseValue | None],
    token_counter=None,
    raise_on_incomplete=False,
) -> _ResponseValue | None:
    """Run one Responses request with bounded retries and per-attempt accounting."""
    from backend.util.case_assistant_workflow import WorkflowStopped
    from backend.util.attachment_parser import WorkPending
    for attempt in range(1, CHAT_COMPLETION_MAX_ATTEMPTS + 1):
        retry_delay = None
        response_id = None
        status = None
        try:
            response = request()
        except APIConnectionError as error:
            record_guided_llm_failure(model=model)
            failure_class = type(error).__name__
            retry_delay = _chat_completion_backoff_seconds(attempt)
        except APIStatusError as error:
            record_guided_llm_failure(model=model)
            failure_class = f"http_{getattr(error, 'status_code', 'unknown')}"
            if not _chat_completion_should_retry_status_error(error):
                logger.warning(
                    "%s terminal model=%s attempt=%s failure_class=%s code=%s",
                    operation, model, attempt, failure_class,
                    _chat_completion_error_code(error),
                )
                return None
            retry_delay = _chat_completion_status_retry_delay(
                error, attempt, operation=operation, model=model,
            )
        except OpenAIError as error:
            if raise_on_incomplete and 'LengthFinishReason' in type(error).__name__:
                from backend.ai.token_budget import IncompleteStructuredOutput
                raise IncompleteStructuredOutput('Structured provider output was incomplete') from error
            record_guided_llm_failure(model=model)
            logger.warning(
                "%s terminal model=%s attempt=%s failure_class=%s",
                operation, model, attempt, type(error).__name__,
            )
            return None
        except (SoftTimeLimitExceeded, WorkflowStopped, WorkPending):
            raise
        except Exception as error:
            record_guided_llm_failure(model=model)
            logger.warning(
                "%s terminal model=%s attempt=%s failure_class=%s",
                operation, model, attempt, type(error).__name__,
            )
            return None
        else:
            record_scoped_llm_response(response, model=model)
            record_guided_llm_response(response, model=model)
            if token_counter is not None:
                token_counter["total"] = (
                    token_counter.get("total", 0)
                    + _chat_completion_response_total_tokens(response)
                )

            response_id = _value_from_object(response, "id")
            status = _value_from_object(response, "status") or "completed"
            if _chat_completion_has_refusal(response):
                failure_class = "refusal"
            elif status == "incomplete":
                if raise_on_incomplete:
                    from backend.ai.token_budget import IncompleteStructuredOutput
                    raise IncompleteStructuredOutput('Structured provider output was incomplete')
                reason = _value_from_object(
                    _value_from_object(response, "incomplete_details"), "reason", "unknown"
                )
                failure_class = f"incomplete:{reason}"
            elif status == "failed":
                code = _chat_completion_response_error_code(response)
                failure_class = f"failed:{code or 'unknown'}"
                if code in _CHAT_COMPLETION_TRANSIENT_RESPONSE_CODES:
                    retry_delay = _chat_completion_backoff_seconds(attempt)
                else:
                    logger.warning(
                        "%s terminal model=%s attempt=%s response_id=%s status=%s failure_class=%s",
                        operation, model, attempt, response_id, status, failure_class,
                    )
                    return None
            elif status != "completed":
                failure_class = f"status:{status}"
            else:
                try:
                    output = extract(response)
                except SoftTimeLimitExceeded:
                    raise
                except Exception as error:
                    logger.warning(
                        "%s terminal model=%s attempt=%s response_id=%s status=%s failure_class=%s",
                        operation, model, attempt, response_id, status,
                        type(error).__name__,
                    )
                    return None
                if output is not None and (
                    not isinstance(output, str) or output.strip()
                ):
                    return output
                failure_class = "empty_output"
                retry_delay = _chat_completion_backoff_seconds(attempt)

            if retry_delay is None:
                logger.warning(
                    "%s terminal model=%s attempt=%s response_id=%s status=%s failure_class=%s",
                    operation, model, attempt, response_id, status, failure_class,
                )
                return None

        if attempt == CHAT_COMPLETION_MAX_ATTEMPTS:
            break
        if retry_delay > CHAT_COMPLETION_MAX_RETRY_DELAY_SECONDS:
            logger.warning(
                "%s terminal model=%s attempt=%s response_id=%s status=%s failure_class=retry_delay_too_long delay=%s",
                operation, model, attempt, response_id, status, retry_delay,
            )
            return None
        logger.warning(
            "%s retry model=%s attempt=%s response_id=%s status=%s failure_class=%s delay=%s",
            operation, model, attempt, response_id, status, failure_class, retry_delay,
        )
        sleep(retry_delay)

    logger.warning(
        "%s attempts_exhausted model=%s attempts=%s",
        operation, model, CHAT_COMPLETION_MAX_ATTEMPTS,
    )
    return None


def chat_completion(
    prompt: str,
    model: str = DEFAULT_MODEL,
    *,
    sys_prompt: str = SYSTEM_PROMPT,
    token_counter=None,
    reasoning_effort: str | None = None,
) -> str | None:
    """Return a free-text Responses API result, or ``None`` on terminal failure."""
    request_kwargs = {
        "model": model,
        "input": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ],
    }
    effective_effort = _effective_reasoning_effort(model, reasoning_effort)
    if effective_effort is not None:
        request_kwargs["reasoning"] = {"effort": effective_effort}
    response = responses_completion(request_kwargs, token_counter=token_counter)
    return _value_from_object(response, "output_text") if response is not None else None


def responses_completion(request_kwargs, *, token_counter=None):
    """Send prepared context and available tool definitions to the LLM.

    Return the complete model response so the controller can read its requested
    tool calls, using the existing shared retry and usage-accounting behavior.
    """
    from backend.util.case_assistant_workflow import WorkflowStopped
    from backend.util.attachment_parser import WorkPending
    from backend.ai.token_budget import validate_text_request
    validate_text_request(request_kwargs)
    model = request_kwargs["model"]
    for attempt in range(1, CHAT_COMPLETION_MAX_ATTEMPTS + 1):
        retry_delay = None
        try:
            logger.info(
                "chat_completion attempt=%s/%s model=%s",
                attempt,
                CHAT_COMPLETION_MAX_ATTEMPTS,
                model,
            )
            # This is the actual LLM API request. A returned tool request still
            # needs to be executed by the agent worker; the API does not run it.
            response = get_openai_client().responses.create(**request_kwargs)
            record_scoped_llm_response(response, model=model)
            record_guided_llm_response(response, model=model)

            total_tokens = _chat_completion_response_total_tokens(response)
            if token_counter is not None:
                token_counter["total"] = token_counter.get("total", 0) + total_tokens

            response_id = _value_from_object(response, "id")
            status = _value_from_object(response, "status") or "completed"

            if _chat_completion_has_refusal(response):
                logger.warning(
                    "chat_completion refusal model=%s attempt=%s response_id=%s status=%s",
                    model,
                    attempt,
                    response_id,
                    status,
                )
                return None

            if status == "incomplete":
                incomplete_details = _value_from_object(
                    response, "incomplete_details"
                )
                incomplete_reason = _value_from_object(
                    incomplete_details, "reason", "unknown"
                )
                logger.warning(
                    "chat_completion incomplete model=%s attempt=%s response_id=%s reason=%s",
                    model,
                    attempt,
                    response_id,
                    incomplete_reason,
                )
                return None

            if status == "failed":
                response_error_code = _chat_completion_response_error_code(response)
                if response_error_code in _CHAT_COMPLETION_TRANSIENT_RESPONSE_CODES:
                    logger.warning(
                        "chat_completion transient_response model=%s attempt=%s response_id=%s code=%s",
                        model,
                        attempt,
                        response_id,
                        response_error_code,
                    )
                    retry_delay = _chat_completion_backoff_seconds(attempt)
                else:
                    logger.warning(
                        "chat_completion terminal_response model=%s attempt=%s response_id=%s status=%s code=%s",
                        model,
                        attempt,
                        response_id,
                        status,
                        response_error_code,
                    )
                    return None
            elif status in {"cancelled", "queued", "in_progress"}:
                logger.warning(
                    "chat_completion unusable_response model=%s attempt=%s response_id=%s status=%s",
                    model,
                    attempt,
                    response_id,
                    status,
                )
                return None
            elif status != "completed":
                logger.warning(
                    "chat_completion unknown_response_status model=%s attempt=%s response_id=%s status=%s",
                    model,
                    attempt,
                    response_id,
                    status,
                )
                return None
            else:
                output_text = _value_from_object(response, "output_text")
                if ((output_text is not None and str(output_text).strip()) or
                        any(_value_from_object(item, "type") == "function_call"
                            for item in (_value_from_object(response, "output", []) or []))):
                    return response
                logger.warning(
                    "chat_completion empty_response model=%s attempt=%s response_id=%s status=%s",
                    model,
                    attempt,
                    response_id,
                    status,
                )
                retry_delay = _chat_completion_backoff_seconds(attempt)

        except APIConnectionError as error:
            record_guided_llm_failure(model=model)
            logger.warning(
                "chat_completion retryable_exception model=%s attempt=%s error_type=%s",
                model,
                attempt,
                type(error).__name__,
            )
            retry_delay = _chat_completion_backoff_seconds(attempt)
        except APIStatusError as error:
            record_guided_llm_failure(model=model)
            status_code = getattr(error, "status_code", None)
            error_code = _chat_completion_error_code(error)
            if not _chat_completion_should_retry_status_error(error):
                logger.warning(
                    "chat_completion terminal_api_error model=%s attempt=%s status_code=%s code=%s error_type=%s",
                    model,
                    attempt,
                    status_code,
                    error_code,
                    type(error).__name__,
                )
                return None

            retry_delay = _chat_completion_status_retry_delay(
                error, attempt, operation="chat_completion", model=model,
            )
            logger.warning(
                "chat_completion retryable_api_error model=%s attempt=%s status_code=%s code=%s error_type=%s",
                model,
                attempt,
                status_code,
                error_code,
                type(error).__name__,
            )
        except OpenAIError as error:
            record_guided_llm_failure(model=model)
            logger.warning(
                "chat_completion terminal_openai_error model=%s attempt=%s error_type=%s",
                model,
                attempt,
                type(error).__name__,
            )
            return None
        except (SoftTimeLimitExceeded, WorkflowStopped, WorkPending):
            raise
        except Exception as error:
            record_guided_llm_failure(model=model)
            logger.warning(
                "chat_completion unexpected_error model=%s attempt=%s error_type=%s",
                model,
                attempt,
                type(error).__name__,
            )
            return None

        if attempt == CHAT_COMPLETION_MAX_ATTEMPTS:
            break
        if retry_delay > CHAT_COMPLETION_MAX_RETRY_DELAY_SECONDS:
            logger.warning(
                "chat_completion retry_delay_too_long model=%s attempt=%s delay=%s",
                model,
                attempt,
                retry_delay,
            )
            return None

        logger.warning(
            "chat_completion retrying model=%s next_attempt=%s delay=%s",
            model,
            attempt + 1,
            retry_delay,
        )
        sleep(retry_delay)

    logger.warning(
        "chat_completion attempts_exhausted model=%s attempts=%s",
        model,
        CHAT_COMPLETION_MAX_ATTEMPTS,
    )
    return None
    


def chat_completion_j(
    prompt: str,
    model=DEFAULT_MODEL,
    sys_prompt=SYSTEM_PROMPT,
    token_counter=None,
    reasoning_effort: str | None = None,
) -> str | None:
    """Deprecated raw-JSON compatibility path for callers without typed schemas."""
    request_kwargs = {
        "model": model,
        "input": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ],
        "text": {"format": {"type": "json_object"}},
    }
    effective_effort = _effective_reasoning_effort(model, reasoning_effort)
    if effective_effort is not None:
        request_kwargs["reasoning"] = {"effort": effective_effort}
    from backend.ai.token_budget import validate_text_request
    validate_text_request(request_kwargs)
    return _responses_with_retries(
        model=model,
        operation="chat_completion_j",
        request=lambda: get_openai_client().responses.create(**request_kwargs),
        extract=lambda response: _value_from_object(response, "output_text"),
        token_counter=token_counter,
    )


def chat_completion_parse(
    prompt: str,
    response_model: type[_ParsedModel],
    *,
    model: str,
    reasoning_effort: str | None,
    sys_prompt: str = SYSTEM_PROMPT,
    token_counter=None,
    max_output_tokens: int | None = None,
    raise_on_incomplete: bool = False,
) -> _ParsedModel | None:
    """Return a validated Pydantic result from a single-turn Responses request."""
    request_kwargs = {
        "model": model,
        "input": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ],
        "text_format": response_model,
    }
    if max_output_tokens is not None:
        request_kwargs["max_output_tokens"] = max_output_tokens
    effective_effort = _effective_reasoning_effort(model, reasoning_effort)
    if effective_effort is not None:
        request_kwargs["reasoning"] = {"effort": effective_effort}
    from backend.ai.token_budget import validate_text_request
    from backend.ai.parse_diagnostics import parse_response
    validate_text_request(request_kwargs)
    return _responses_with_retries(
        model=model,
        operation="chat_completion_parse",
        request=lambda: parse_response(get_openai_client(), request_kwargs),
        extract=lambda response: _value_from_object(response, "output_parsed"),
        token_counter=token_counter,
        raise_on_incomplete=raise_on_incomplete,
    )


def _multichat_messages(
    prev_prompt_output: list[tuple[str, str]],
    new_prompt: str,
    *,
    sys_prompt: str,
    context_message: str | None,
) -> list[dict[str, str]]:
    """Preserve the legacy message ordering shared by all multichat variants."""
    messages = [{"role": "system", "content": sys_prompt}]
    if context_message:
        messages.append({"role": "user", "content": context_message})
        messages.append({
            "role": "assistant",
            "content": "Understood. I will use this session context.",
        })
    for each_prompt, each_output in prev_prompt_output:
        messages.append({"role": "user", "content": f' "{each_prompt}"'})
        messages.append({"role": "assistant", "content": f' "{each_output}"'})
    messages.append({"role": "user", "content": f' "{new_prompt}"'})
    return messages


def multichat_completion_text(
    prev_prompt_output: list[tuple[str, str]],
    new_prompt: str,
    *,
    model: str,
    reasoning_effort: str | None,
    sys_prompt: str = SYSTEM_PROMPT,
    context_message: str | None = None,
    token_counter=None,
) -> str | None:
    """Return free text from a multi-turn Responses request."""
    request_kwargs = {
        "model": model,
        "input": _multichat_messages(
            prev_prompt_output,
            new_prompt,
            sys_prompt=sys_prompt,
            context_message=context_message,
        ),
    }
    effective_effort = _effective_reasoning_effort(model, reasoning_effort)
    if effective_effort is not None:
        request_kwargs["reasoning"] = {"effort": effective_effort}
    from backend.ai.token_budget import validate_text_request
    validate_text_request(request_kwargs)
    return _responses_with_retries(
        model=model,
        operation="multichat_completion_text",
        request=lambda: get_openai_client().responses.create(**request_kwargs),
        extract=lambda response: _value_from_object(response, "output_text"),
        token_counter=token_counter,
    )


def multichat_completion_parse(
    prev_prompt_output: list[tuple[str, str]],
    new_prompt: str,
    response_model: type[_ParsedModel],
    *,
    model: str,
    reasoning_effort: str | None,
    sys_prompt: str = SYSTEM_PROMPT,
    context_message: str | None = None,
    token_counter=None,
) -> _ParsedModel | None:
    """Return a validated Pydantic result from a multi-turn Responses request."""
    request_kwargs = {
        "model": model,
        "input": _multichat_messages(
            prev_prompt_output,
            new_prompt,
            sys_prompt=sys_prompt,
            context_message=context_message,
        ),
        "text_format": response_model,
    }
    effective_effort = _effective_reasoning_effort(model, reasoning_effort)
    if effective_effort is not None:
        request_kwargs["reasoning"] = {"effort": effective_effort}
    from backend.ai.token_budget import validate_text_request
    validate_text_request(request_kwargs)
    return _responses_with_retries(
        model=model,
        operation="multichat_completion_parse",
        request=lambda: get_openai_client().responses.parse(**request_kwargs),
        extract=lambda response: _value_from_object(response, "output_parsed"),
        token_counter=token_counter,
    )


def multichat_completion_j(
    prev_prompt_output: list[tuple[str, str]],
    new_prompt: str,
    model=DEFAULT_MODEL,
    sys_prompt=SYSTEM_PROMPT,
    require_json: bool = True,
    context_message: str | None = None,
    token_counter=None,
    *,
    reasoning_effort: str | None = None,
) -> str | None:
    """Deprecated raw-JSON compatibility path for existing multichat callers."""
    if not require_json:
        return multichat_completion_text(
            prev_prompt_output,
            new_prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            sys_prompt=sys_prompt,
            context_message=context_message,
            token_counter=token_counter,
        )
    request_kwargs = {
        "model": model,
        "input": _multichat_messages(
            prev_prompt_output,
            new_prompt,
            sys_prompt=sys_prompt,
            context_message=context_message,
        ),
        "text": {"format": {"type": "json_object"}},
    }
    effective_effort = _effective_reasoning_effort(model, reasoning_effort)
    if effective_effort is not None:
        request_kwargs["reasoning"] = {"effort": effective_effort}
    from backend.ai.token_budget import validate_text_request
    validate_text_request(request_kwargs)
    return _responses_with_retries(
        model=model,
        operation="multichat_completion_j",
        request=lambda: get_openai_client().responses.create(**request_kwargs),
        extract=lambda response: _value_from_object(response, "output_text"),
        token_counter=token_counter,
    )


# Web search helper for citation-sensitive workflows.
#
# - Uses the Responses API `web_search` tool (not preview) so citations/sources are available.
# - For legal-citation accuracy, you typically want the model to cite (and ideally quote) the
#   exact source page where the citation appears.
def web_search_completion(
    prompt: str,
    model: str = DEFAULT_MODEL,
    allowed_domains: Optional[list[str]] = None,
    return_meta: bool = False,
    reasoning_effort: str | None = None,
    *,
    sys_prompt: str | None = None,
    prev_prompt_output: list[tuple[str, str]] | None = None,
):
    """Run a web-enabled response and return the text.

    Optionally returns best-effort source/annotation metadata for callers that
    need to verify exact matches.
    """
    tool = {"type": "web_search"}
    if allowed_domains:
        tool["filters"] = {"allowed_domains": allowed_domains[:100]}

    messages = []
    if sys_prompt is not None:
        messages.append({"role": "system", "content": sys_prompt})
    for previous_prompt, previous_output in prev_prompt_output or []:
        messages.append({"role": "user", "content": previous_prompt})
        messages.append({"role": "assistant", "content": previous_output})
    messages.append({"role": "user", "content": prompt})

    request_kwargs = {
        "model": model,
        "input": messages,
        "tools": [tool],
        "include": ["web_search_call.action.sources"],
    }
    effective_effort = _effective_reasoning_effort(model, reasoning_effort)
    if effective_effort is not None:
        request_kwargs["reasoning"] = {"effort": effective_effort}

    for attempt in range(1, CHAT_COMPLETION_MAX_ATTEMPTS + 1):
        try:
            resp = get_openai_client().responses.create(**request_kwargs)
        except APIConnectionError as error:
            record_guided_llm_failure(model=model)
            last_error = error
            retry_delay = _chat_completion_backoff_seconds(attempt)
        except APIStatusError as error:
            record_guided_llm_failure(model=model)
            if not _chat_completion_should_retry_status_error(error):
                raise
            last_error = error
            retry_delay = _chat_completion_status_retry_delay(
                error, attempt, operation="web_search_completion", model=model,
            )
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            record_guided_llm_failure(model=model)
            raise
        else:
            record_scoped_llm_response(resp, model=model)
            record_guided_llm_response(resp, model=model)
            break

        if attempt == CHAT_COMPLETION_MAX_ATTEMPTS or retry_delay > CHAT_COMPLETION_MAX_RETRY_DELAY_SECONDS:
            raise last_error
        logger.warning(
            "web_search_completion retry model=%s attempt=%s failure_class=%s delay=%s",
            model, attempt, type(last_error).__name__, retry_delay,
        )
        sleep(retry_delay)

    output_text = _value_from_object(resp, "output_text", "") or ""
    if not return_meta:
        return output_text

    sources = []
    annotations = []
    try:
        out_items = _value_from_object(resp, "output") or []
        for item in out_items:
            item_type = _value_from_object(item, "type")
            if item_type == "web_search_call":
                action = _value_from_object(item, "action")
                sources.extend(_value_from_object(action, "sources") or [])
            elif item_type == "message":
                for content_item in _value_from_object(item, "content") or []:
                    if _value_from_object(content_item, "type") == "output_text":
                        annotations.extend(
                            _value_from_object(content_item, "annotations") or []
                        )
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        pass

    return output_text, {
        "sources": sources if sources else _value_from_object(resp, "sources"),
        "annotations": annotations,
    }



def grok_completion(prompt: str, model: str = "grok-4"):
    """Compatibility wrapper for older callers that still import grok_completion.

    If an XAI API key is configured we call xAI directly; otherwise we fall back
    to the repo's default chat completion client so imports and tests still work.
    """
    xai_api_key = os.getenv("XAI_API_KEY")
    if xai_api_key:
        xai_client = OpenAI(
            api_key=xai_api_key,
            base_url="https://api.x.ai/v1",
        )
        resp = xai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a concise legal assistant."},
                {"role": "user", "content": prompt},
            ],
        )
        return resp.choices[0].message.content

    return chat_completion(prompt)



def deepseek_completion_j(prompt: str, system_prompt: str=SYSTEM_PROMPT, model: str="deepseek/deepseek-r1:free"):
    # deepseek-chat: v3
    # deepseek-reasoner: R1
    client = OpenAI(
        api_key=os.getenv("OPENROUTER_API_KEY"), 
        base_url="https://openrouter.ai/api/v1"
        )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        extra_headers={},
        response_format={"type":"json_object"},
        stream=False
    )

    if response and response.choices:
        message = response.choices[0].message
        if message and message.content:
            print(message.content)
            return message.content
            
    logging.error("DeepSeek API returned empty response")
    return ""


def extract_headings(
    body_text_lines: list[str], *, include_adjacent_lines: bool = False,
):
    """Get heading lines, optionally including nearby lines for multi-line headings."""
    def is_heading(line: str) -> bool:
        return line.isupper() or (line.startswith('<B>') and line.endswith('</B>'))

    def starts_split_bold_heading(line: str) -> bool:
        return line.startswith('<B>') and '</B>' not in line

    heading_indexes = [
        line_num for line_num, line in enumerate(body_text_lines)
        if (is_heading(line) or (include_adjacent_lines and starts_split_bold_heading(line)))
        and line.replace('<B>', '').replace('</B>', '').strip() != "COMPLAINT"
    ]
    candidate_indexes = set(heading_indexes)
    if include_adjacent_lines:
        for heading_index in heading_indexes:
            line_num = heading_index + 1
            adjacent_count = 0
            inside_split_bold = starts_split_bold_heading(body_text_lines[heading_index])
            while line_num < len(body_text_lines):
                line = body_text_lines[line_num]
                if (
                    not line.strip()
                    or is_heading(line)
                    or starts_split_bold_heading(line)
                    or re.match(r"^\s*(?:\d+[.),]|\(\d+\))(?:\s|$)", line)
                    or (adjacent_count >= 2 and not inside_split_bold)
                ):
                    break
                candidate_indexes.add(line_num)
                adjacent_count += 1
                if '</B>' in line:
                    inside_split_bold = False
                line_num += 1

    line_numbers = sorted(candidate_indexes)
    headings = [
        body_text_lines[line_num].replace('<B>', '').replace('</B>', '')
        for line_num in line_numbers
    ]
    return headings, line_numbers


def num_tokens_from_string(string: str, encoding_name="cl100k_base") -> int:
    """Returns the number of tokens in a text string."""
    # 'o200k_base' = gpt-4o, gpt-4o-mini  (# ValueError: Unknown encoding o200k_base. Plugins found: ['tiktoken_ext.openai_public'])
    # 'cl100k_base' = gpt-4-turbo, gpt-4, gpt-3.5-turbo
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens


def truncate_text_to_tokens(text: str, max_tokens: int, encoding_name="cl100k_base") -> str:
    """Truncate text to at most max_tokens using tiktoken."""
    if not text or max_tokens <= 0:
        return ""
    encoding = tiktoken.get_encoding(encoding_name)
    tokens = encoding.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return encoding.decode(tokens[:max_tokens]) + "..."


@retry(wait=wait_fixed(1))
def get_embedding(text: str, model="text-embedding-3-small")-> list[float]:
    # replace newlines, which can negatively affect performance.
    text = text.replace("\n", " ")
    return get_openai_client().embeddings.create(input=[text], model=model).data[0].embedding


def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))


class HeadingSectionResult(BaseModel):
    section_headings: str = Field(alias="section headings")
    first_heading_of_next_section: str = Field(alias="first heading of next section")


def start_end_from_headings(
    prompt_text: str,
    hd: list[str],
    ln: list[int],
    *,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = None,
) -> list[int]:
    """ send raw heading and line number to llm, allow multiple line returned for "section heading" and one line returned\
    for next section heading, return start_end_i as a list of line number in the original body text"""
    # print("inside start_end_from_headings")
    # print("=== get list prompt ===\n", prompt_text)
    result = chat_completion_parse(
        prompt_text,
        HeadingSectionResult,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    if result is None:
        raise ValueError("No parsed section headings returned.")

    start_end_i = []
    headings = result.section_headings
    next_heading = result.first_heading_of_next_section
    print("headings: ", headings)
    print("next_heading: ", next_heading)

    """ if there are 3 cause of action headings, and next is prayer for relief
    then start_end_i = [#1, #2, #3, #prayer]
    """
    line_num_pattern = r"\(([0-9]+)\)"
    print("decoding this section headings and line numbers")
    for h in headings.split(';'):
        print("for this section: start_end_i")
        matches = list(re.finditer(line_num_pattern, h))
        # If matches were found, print the last one
        if matches:
            last_match = matches[-1]
            print("Last matched part:", last_match.group(1))
            start_end_i.append(int(last_match.group(1)))
        else:
            print("line number not found")

    if len(start_end_i) < 1:
        raise ValueError("No line number returned for this entire section.")
    else:
        # look for next section, if not found, append with last heading's line number
        # print("decoding next section heading and line number")
        matches = list(re.finditer(line_num_pattern, next_heading))
        if matches:
            start_end_i.append(int(matches[-1].group(1)))
            # print("add (new) next section: ", start_end_i)
        else:
            # if llm does not return next heading, append line number for the last raw heading
            start_end_i.append(ln[-1])
            # print("next section not found, add last heading line number: ", start_end_i)

        if start_end_i[-1] - start_end_i[0] < 2:
            raise ValueError("no lines between start and end line")
    
    # add a step for cases like:
    # Final start_end_i:  [487, 488, 565, 634]
    # headings:  CAUSES OF ACTION     (487); COUNT I  (488); COUNT II (565)
    # next_heading:  PRAYER FOR RELIEF        (634)
    print("start_end_i: ", start_end_i) 
    final_start_end_i = []
    for idx, line_num in enumerate(start_end_i):
        if idx == 0:
            final_start_end_i.append(line_num)
        else:
            prev_line_num = start_end_i[idx-1]
            if line_num - prev_line_num < 3:  # means these two heading are continuous line, or apart just by one line (likely A. )
                continue
            else:
                final_start_end_i.append(line_num)
    print("Final start_end_i: ", final_start_end_i)              
    return final_start_end_i


def get_fa_loc(body_lines:list[str]) -> list[int]:
    section_name = "Facts"
    next_section = "Cause of Action"
    """From the complaint text gets the FA text by lines."""
    hd, ln = extract_headings(body_lines)

    if len(hd) < 5:
        print("Warning: The number of headings is less than 5. Whole document returned")
        # return body_lines
        return [0, len(body_lines)-1]

    hd_lines = "\n".join(str(h) + "\t(" + str(l) + ")" for h, l in list(zip(hd, ln)))
    # start with heading lines
    comand_prompt = open('backend/ai/prompts/rfa/FIND_SEC').read()
    filled_in_prompt = hd_lines + f" The whole document has {len(body_lines)} lines. " + comand_prompt.format(
        section=section_name, next_section=next_section
    ) + open('backend/ai/prompts/rfa/FIND_SEC_J').read()
    print("get_fa_filled_in_prompt: ", filled_in_prompt)
    try:
        print("first try")
        start_end_i = start_end_from_headings(filled_in_prompt, hd, ln)
    except Exception as e:
        print(e)
        print("second try")
        try:
            start_end_i = start_end_from_headings(filled_in_prompt, hd, ln)
        except Exception as e:
            print(e)
            # if it fails twice just include the whole complaint
            start_end_i = [0, len(body_lines)-1]
            print("include whole doc: ", start_end_i)

    # start_i = start_end_i[0]
    # end_i = start_end_i[-1]
    # print(f"start_i: {start_i}, end_i: {end_i}")
    # return [start_i, end_i]
    return start_end_i   # return a list of integer that are be used to segment the fa
    # return body_lines[start_i:end_i]


def get_fa_text(body_lines:list[str]) -> list[str]:
    '''Given a list of integer of line numbers: [10, 12, 14, 19, 20], return body lines from 10 to 20'''
    start_end_i = get_fa_loc(body_lines)
    start_i = start_end_i[0]
    end_i = start_end_i[-1]
    return body_lines[start_i:end_i]


def get_coa_loc(
    body_lines: list[str],
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> list[int]:
    section_name = "Cause of Action"
    next_section = "Prayer For Relief"
    """From the complaint text gets the FA section text by lines."""
    hd, ln = extract_headings(body_lines)

    if len(hd) < 5:
        print("Warning: The number of headings is less than 5. Whole document returned")
        # coa_lines_blocks.append(body_lines)
        return [0, len(body_lines)-1]
    hd, ln = extract_headings(body_lines, include_adjacent_lines=True)
    
    hd_lines = "\n".join(str(h) + "\t(" + str(l) +")" for h, l in list(zip(hd,ln)))
    # start with heading lines
    comand_prompt = open('backend/ai/prompts/rfa/FIND_SEC').read()
    filled_in_prompt = hd_lines + f"\nThe whole document has {len(body_lines)} lines.\n" + comand_prompt.format(
        section=section_name, next_section=next_section,
    ) + open('backend/ai/prompts/rfa/FIND_SEC_J').read()
    logger.info("get_coa_loc assembled heading prompt:\n%s", filled_in_prompt)
    completion_kwargs = {}
    if model is not None:
        completion_kwargs["model"] = model
    if reasoning_effort is not None:
        completion_kwargs["reasoning_effort"] = reasoning_effort
    try:
        print("first try")
        start_end_i = start_end_from_headings(filled_in_prompt, hd, ln, **completion_kwargs)
    except Exception as e:
        print(e)
        try:
            print("second try")
            start_end_i = start_end_from_headings(filled_in_prompt, hd, ln, **completion_kwargs)
        except Exception as e:
            print(e)
            # if it fails twice just include the whole complaint
            print("failed")
            start_end_i = [0, len(body_lines)-1]
            print("include whole doc: ", start_end_i)
    return start_end_i


def get_coa_text(body_lines: list[str]) -> list[list[str]]:
    start_to_end_i = get_coa_loc(body_lines)
    coa_lines_blocks = []
    for i in range(len(start_to_end_i)-1):
        start_i = start_to_end_i[i]
        end_i = start_to_end_i[i+1]
        coa_lines_blocks.append(body_lines[start_i: end_i])

    return coa_lines_blocks


def generate_coa_summary(content: str) -> str:
    """ Summarize COA title out of a chunk of coa text """
    command_prompt = open('backend/ai/prompts/rfp/GET_COA_TITLE').read()
    user_prompt = command_prompt + " ".join(content)
    summary = chat_completion(user_prompt)
    return summary



def get_rfp_section_text(body_lines: list[str], section: str) -> list[int]:
    """From the RFP text or the whole body text, get request items list."""
    hd, ln = extract_headings(body_lines)
    hd_lines = "\n".join(str(h) + "\t(" + str(l) + ")" for h, l in list(zip(hd, ln)))
    print("=== headings and line numbers ===\n", hd_lines)

    if len(hd) < 5:
        print("Warning: The number of headings is less than 5. Whole document returned")
        return [0, len(body_lines)-1]

    comand_prompt = open('backend/ai/prompts/rfp_response/FIND_RFP_SEC').read()
    filled_in_prompt = comand_prompt.format(raw_headings=hd_lines, section=section)
    try:
        print("first try")
        start_end_i = start_end_from_headings(filled_in_prompt, hd, ln)
    except Exception as e:
        print(e)
        try:
            print("second try")
            start_end_i = start_end_from_headings(filled_in_prompt, hd, ln)
        except Exception as e:
            print(e)
            # if it fails twice just include the whole complaint
            print("failed")
            start_end_i = [0, ln[-1]]
            print("include whole doc: ", start_end_i)

    return start_end_i


def get_venue_text(body_text:str) -> list[str]:
    section_name = "Venue"
    next_section = "Facts"
    """From the complaint text gets the FA text by lines."""
    body_lines = body_text.split('\n')
    hd, ln = extract_headings(body_lines)

    if len(hd) < 5:
        print("Warning: The number of headings is less than 5. Whole document returned")
        return body_lines

    hd_lines = "\n".join(str(h) + "\t(" + str(l) + ")" for h, l in list(zip(hd, ln)))
    # start with heading lines
    comand_prompt = open('backend/ai/prompts/rfa/FIND_SEC').read()
    filled_in_prompt = hd_lines + f" The whole document has {len(body_lines)} lines. " + comand_prompt.format(
        section=section_name, next_section=next_section
    ) + open('backend/ai/prompts/rfa/FIND_SEC_J').read()

    print("== get_venue_filled_in_prompt ==\n", filled_in_prompt)

    try:
        print("first try")
        start_end_i = start_end_from_headings(filled_in_prompt, hd, ln)
    except Exception as e:
        print(e)
        print("second try")
        try:
            start_end_i = start_end_from_headings(filled_in_prompt, hd, ln)
        except Exception as e:
            print(e)
            # if it fails twice just include the whole complaint
            start_end_i = [0, ln[-1]]
            print("include whole doc: ", start_end_i)

    start_i = start_end_i[0]
    end_i = start_end_i[-1]
    print(f"start_i: {start_i}, end_i: {end_i}")

    return body_lines[start_i:end_i]


class PartiesVenueResult(BaseModel):
    plaintiff: list[str]
    defendant: list[str]
    venue: str


def extract_parties_venue(
    body_text: str,
    *,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = "medium",
) -> tuple[list[str], list[str], str]:
    comand_prompt = open("backend/ai/prompts/answer/EXTRACT_N_PARTIES").read()
    filled_in_prompt = comand_prompt.format(complaint=body_text)
    print("== EXTRACT_N_PARTIES filled_in_prompt ==\n", filled_in_prompt)
    result = chat_completion_parse(
        filled_in_prompt,
        PartiesVenueResult,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    if result is None:
        return [], [], ""
    return result.plaintiff, result.defendant, result.venue


def get_headers_from_llm(text_by_lines: list[str]) -> dict:
    """
    Extract all section headings as long as they're Bold lines or all in upper-case.
    Returns:
    hd_2_ln : list[("HEADING A", line_num)]   if llm can detect 5+ headings, otherwise []
    """
    # print("=== text by lines ===\n", text_by_lines)
    hd, ln_numbers = extract_headings(text_by_lines)
    print(hd)
    print(ln_numbers)
    hd_lines = "\n".join(str(h) + "\t(" + str(l) + ")" for h, l in list(zip(hd, ln_numbers)))
    command_prompt = open('backend/ai/prompts/answer/EXTRACT_HEADINGS').read()
    filled_in_prompt = command_prompt.format(hd_lines=hd_lines)
    print("== Extract headers filled_in_prompt ===\n", filled_in_prompt)
    llm_output = chat_completion_j(filled_in_prompt)
    print("== Extract headers llm_output ===\n", llm_output)

    line_num_pattern = r"\(([0-9]+)\)"
    heading_cutoffs = [0]
    hd_2_ln = {}
    try:
        headings_str = json.loads(llm_output)["headings"]
        print("lm output headings_str: \n", headings_str)
        for head_line in headings_str.split(';'):
            matches = list(re.finditer(line_num_pattern, head_line))
            # If matches were found, print the last one
            if matches:
                matched_num = int(matches[-1].group(1))
                print("Last matched line number:", matched_num)
                heading = head_line[:head_line.rfind('(')].strip()
                if matched_num > heading_cutoffs[-1]:  # new added line number has to be greater than the current last
                    heading_cutoffs.append(matched_num)
                    print("New section headers added: ", heading)
                    hd_2_ln[heading] = matched_num
    except Exception as e:
        print("Error decoding heading line numbers: ", e)
    print("heading cutoffs: ", heading_cutoffs)
    return hd_2_ln if len(hd_2_ln) > 4 else {}


def get_paralines_from_llm(text_by_lines: list[str]) -> dict:
    """
    Make llm call using extracted raw header lines (2 consecutive lines start with para_pattern)
    Returns:
    para_2_ln: dict
    - key (int): para_number
    - value (int): line_number (within whole doc)
    """
    para_pattern = r'^(\d{1,4})[^a-zA-Z0-9]+[A-Z]'
    raw_para_2_ln = []
    for ln_num in range(len(text_by_lines)-1):
        line = text_by_lines[ln_num]
        next_line = text_by_lines[ln_num+1]
        new_line = ' '.join([line, next_line])

        match1 = re.match(para_pattern, new_line)  # match since the start
        if match1:
            # matched_num = int(match1.group(1))
            # print(f"match1: >{line}<")
            raw_para_2_ln.append(f'line_number: {ln_num}, line_text: {new_line}')

    command = open("backend/ai/prompts/answer/EXTRACT_PARA").read()
    filled_in_prompt = command.format(total_ln=len(text_by_lines), para_2_ln='\n'.join(raw_para_2_ln))
    print("=== raw para lines filled_in_prompt ===\n", filled_in_prompt)
    llm_output = chat_completion_j(prompt=filled_in_prompt)
    print("== para output ==\n", llm_output)

    para_2_ln = {}    # # for complaint that contains a table content at front, a later para1 will rewrite previous para1
    try:
        llm_output_list = extract_from_json("paragraphs", llm_output)
        for each in llm_output_list:
            # print("para: ", each["paragraph number"])
            # print("line: ", text_by_lines[int(each["line number"]): int(each["line number"])+2])
            try:
                para_2_ln[int(each["paragraph number"])] = int(each["line number"])
            except KeyError as e:
                print("KeyError looking for paragraph number or line number: ", e)
    except Exception as e:
        print("Error encoding para llm outputa as a list")

    return para_2_ln



def longest_inc_sequence(nums: list[int]) -> list[int]:
    if not nums:
        return []
    n = len(nums)
    dp = [1] * n   # dp[i] represents the length of the LIS ending at index i
    prev = [-1] * n  # To reconstruct the sequence, store the previous index
    # Calculate LIS length for each element
    for i in range(1, n):
        for j in range(i):
            if nums[i] > nums[j] and dp[i] < dp[j] + 1:
                dp[i] = dp[j] + 1
                prev[i] = j

    # Find the index of the maximum LIS length
    max_index = dp.index(max(dp))
    # Reconstruct the sequence
    lis = []
    while max_index != -1:
        lis.append(nums[max_index])
        max_index = prev[max_index]

    return lis[::-1]  # Reverse to get the correct order


def is_consecutive_or_expand(parag_2_ln:dict) -> dict:

    key_lst = list(parag_2_ln.keys())
    print("check consecutive: before:\n", key_lst)
    for i in range(1, len(key_lst)):
        diff = key_lst[i] - key_lst[i-1]
        diff_break = 1/diff
        prev_key = key_lst[i-1]
        line_diff = parag_2_ln[key_lst[i]] - parag_2_ln[key_lst[i-1]]  
        while diff > 1:
            new_key = prev_key + 1
            parag_2_ln[new_key] = parag_2_ln[prev_key] + int(diff_break * line_diff)   # a fake (evenly break) in line numbers
            print("New key added: ", new_key, "; new value: ", parag_2_ln[new_key])
            prev_key = new_key
            diff -= 1

    print("check consecutive: after:\n", parag_2_ln.keys())
    return parag_2_ln

def split_into_chunks(text_lines:list[str], chunk_len=5000, overlap_lines=5) -> list[list[str]]:  # default to 5000 chars (that is <1000 tokens)
    text_lines_chunks = []
    i = 0
    curr_chunk = []
    chunk_total_chars = 0
    while i < len(text_lines):
        to_add = len(text_lines[i])   #  #chars to be added in the next line
        if chunk_total_chars + to_add <= chunk_len:   
            curr_chunk.append(text_lines[i])     
            chunk_total_chars += to_add
            i += 1
        else:
            i -= overlap_lines   # roll back to 5 lines before (create some overlap between chunks)
            text_lines_chunks.append(curr_chunk)   # add current chunk to destiny list
            curr_chunk = []                        # reset current chunk
            chunk_total_chars = 0                  # reset current chunk total chars
    
    if curr_chunk:        # add any outstanding curr_chunk
        text_lines_chunks.append(curr_chunk)
    
    return text_lines_chunks


def extract_raw_headings_and_nextline(body_text_lines: list[str]):

    """Gets the headings and line numbers, and the following line."""
    line_numbers = []
    headings = []
    nextlines = []
    for line_num, line in enumerate(body_text_lines):
        
        if line.isupper() or (line.startswith('<B>') and line.endswith('</B>')):
            # take off markdown
            line = line.replace('<B>', '').replace('</B>', '')
            if line.strip() == "COMPLAINT":
                continue
            else:
                headings.append(line)
                line_numbers.append(line_num)
                nextlines.append(' '.join(body_text_lines[line_num+1: line_num+3]))


    return headings, line_numbers, nextlines


def get_headers_before_coa(
    body_lines: list[str],
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> list[int]:
    """From the complaint text gets all heading and sub-headings before coa."""
    hd, ln, nextlines = extract_raw_headings_and_nextline(body_lines)

    if len(hd) < 5:
        print("Warning: The number of headings is less than 5. Whole document returned")
        # return body_lines
        return [0, len(body_lines)-1]

    hd_lines = "\n".join(str(h) + "\t(" + str(l) + ")\tfollowing line: "+ str(nextline) for h, l, nextline in list(zip(hd, ln, nextlines)))
    # start with heading lines
    comand_prompt = open('backend/ai/prompts/element_checker/FIND_HEADERS_BF_COA').read()
    filled_in_prompt = hd_lines + f"\nThe whole document has {len(body_lines)} lines.\n" + comand_prompt
    print("get_HEADERS_BF_COA_filled_in_prompt: ", filled_in_prompt)
    completion_kwargs = {}
    if model is not None:
        completion_kwargs["model"] = model
    if reasoning_effort is not None:
        completion_kwargs["reasoning_effort"] = reasoning_effort
    llm_output = chat_completion_j(filled_in_prompt, **completion_kwargs)
    print(f"***{llm_output}***")
    llm_list = extract_from_json('headings before cause of action', llm_output)
    llm_coa_h = extract_from_json('first heading of cause of action', llm_output)

    start_end_i = []
    if llm_list and llm_coa_h:
        line_num_pattern = r"\(([0-9]+)\)"
        print("decoding this section headings and line numbers")
        for h in llm_list.split(';'):
            print("for this section: start_end_i")
            matches = list(re.finditer(line_num_pattern, h))
            # If matches were found, print the last one
            if matches:
                last_match = matches[-1]
                print("Last matched part:", last_match.group(1))
                start_end_i.append(int(last_match.group(1)))
            else:
                print("line number not found")

        if len(start_end_i) < 1:
            # raise ValueError("No line number returned for this entire section.")
            final_start_end_i = [0, len(body_lines)-1]
            print("include whole doc: ", final_start_end_i)
            
        else:
            # look for next section, if not found, append with last heading's line number
            # print("decoding next section heading and line number")
            matches = list(re.finditer(line_num_pattern, llm_coa_h))
            if matches:
                start_end_i.append(int(matches[-1].group(1)))
                # print("add (new) next section: ", start_end_i)
            else:
                # if llm does not return next heading, append line number for the last raw heading
                start_end_i.append(ln[-1])
                # print("next section not found, add last heading line number: ", start_end_i)

            if start_end_i[-1] - start_end_i[0] < 2:
                raise ValueError("no lines between start and end line")

        # add a step for cases like:
        # Final start_end_i:  [487, 488, 565, 634]
        # headings:  CAUSES OF ACTION     (487); COUNT I  (488); COUNT II (565)
        # next_heading:  PRAYER FOR RELIEF        (634)
        print("start_end_i: ", start_end_i) 
        final_start_end_i = []
        for idx, line_num in enumerate(start_end_i):
            if idx == 0:
                final_start_end_i.append(line_num)
            else:
                prev_line_num = start_end_i[idx-1]
                if line_num - prev_line_num < 3:  # means these two heading are continuous line, or apart just by one line (likely A. )
                    continue
                else:
                    final_start_end_i.append(line_num)
        print("Final start_end_i: ", final_start_end_i)
    else:
        final_start_end_i = [0, len(body_lines)-1]
        print("either llm_list or llm_coa_h is empty, include whole doc: ", final_start_end_i)

    # Validate that all indices are within bounds of body_lines
    max_line_count = len(body_lines)
    if any(idx >= max_line_count or idx < 0 for idx in final_start_end_i):
        print(f"Warning: Some final_start_end_i indices are out of bounds (max={max_line_count}). Clamping values.")
        final_start_end_i = [max(0, min(idx, max_line_count - 1)) for idx in final_start_end_i]
    
    return final_start_end_i  


class RFPRequestItem(BaseModel):
    request_id: str = Field(
        description=(
            "The exact identifier of the request from the raw text. It may be a "
            "number, letter, or label such as 'First Request'."
        )
    )
    request: str = Field(
        description="The exact text of the request, unaltered."
    )


class RFPRequestList(BaseModel):
    requests: list[RFPRequestItem] = Field(
        description="Every identified Request for Production item."
    )


def chat_completion_j_list_requests(
    user_prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = "medium",
) -> dict[str, list[dict[str, str]]] | None:
    """Extract RFP requests with the Responses API and Structured Outputs."""
    parsed_output = chat_completion_parse(
        user_prompt,
        RFPRequestList,
        model=model,
        reasoning_effort=reasoning_effort,
        sys_prompt="You are a JSON‐only extractor. When given raw text of Request for Production items, identify each individual request and return a JSON array of objects with `request_id` and `request` fields.",
    )
    return parsed_output.model_dump() if parsed_output is not None else None


class TitlePartiesVenueResult(BaseModel):
    plaintiff: list[str]
    defendant: list[str]
    title: str
    venue: str


def extract_title_parties_venue(
    body_text: str,
    *,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = "medium",
) -> tuple[list[str], list[str], str, str]:
    comand_prompt = open("backend/ai/prompts/rog_response/EXTRACT_TITLE_PARTIES").read()
    filled_in_prompt = comand_prompt.format(complaint=body_text)
    result = chat_completion_parse(
        filled_in_prompt,
        TitlePartiesVenueResult,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    if result is None:
        return [], [], "", ""
    return result.plaintiff, result.defendant, result.title, result.venue



if __name__ == "__main__":
    import time
    # facts = """
    # - Plaintiff resides in Los Angeles, California.                    
    # - Plaintiff is a multiple Oscar winning and nominated writer and director.                    
    # - Plaintiff wrote a screenplay called: The Hateful Eight'                    
    # - Plaintiff routinely sells screenplays for money.                      
    # - On Jan 22, 2014 the defendant solicited its readers to infringe on the plaintiff's copyright inducing anyone to provide the defendant with an unauthorized version of the screenplay.                    
    # - On Jan 23 the defendant received a copy of the screenplay, and downloaded the screenplay.                    
    # - On Jan 23 the defendant published an article titled "Here Is the Leaked Quentin Tarantino Hateful Eight Script" with a link to illegally download the screenplay.                    
    # - Gawker's conduct constitutes a direct copyright infringement of Plaintiff's rights and copyright.                    
    # - Gawker then utilized the unauthorized infringing PDF copy of the Screenplay to disseminate portions of and/or content from the unpublished work.                    
    # - On Jan 26 Gawker amended the article with additional URL to download the screenplay.                    
    # - Gawker through its article and specific URL click-through links directed the general public on where and how to obtain and download an otherwise unknown unauthorized copyright infringing PDF copy of the unreleased Screenplay.                    
    # - Gawker's conduct constitutes a contributory copyright infringement of Plaintiff's rights and copyright.                    
    # - On January 23, 2014 (and again on January 24, 2014), Plaintiff's authorized agent submitted a "Digital Millennium Copyright Act ("DMCA") Notice of Infringement" to the operators of the Gawker.                    
    # - Gawker failed and refused to comply with the Gawker DMCA Notice, and refused to remove from its website the direct URL links

    #  Plaintiff: Quentin Tarantino 
    # Defendant: GAWKER MEDIA, LLC

    # """
    # venue = "UNITED STATES DISTRICT COURT CENTRAL DISTRICT OF CALIFORNIA"
 
    # # complaint_text, case_number, _ = get_text_from_pdf("/Users/pbharrin/Downloads/GWGComplaint_CLEAN_FULL.pdf")
    # start_time = time.time()
    # coa_template = open('backend/ai/prompts/complaint/SUGGEST_COA').read()
    # comand_prompt = coa_template.format(venue, facts)
    # results = chat_completion(comand_prompt)
    # end_time = time.time()
    # print(f"The function took {end_time - start_time} seconds to complete.")
    # print(results)

    # comand_prompt = open('backend/ai/prompts/complaint/DRAFT_INTRO').read()
    # filled_in_prompt = comand_prompt.format(facts_and_parties=facts)
    # # print(complaint_text)
    # start_time = time.time()
    # results = chat_completion_j(filled_in_prompt)
    # print(results)
    # end_time = time.time()
    # print(f"The function took {end_time - start_time} seconds to complete.")


# ======== Complaint Files for testing   =========#
    # filename = "jl/JCOC v. US (LCCRSF).pdf"
    # filename = "jl/JL Complaint - State Court (Ferguson).pdf"
    # filename = "Gallagher_v_Edgewood_1st_Complaint.pdf"
    # filename = "Meta - COMPLAINT.pdf"
    # filename = "AAFER v Fearless (COMPLAINT).pdf"
    # filename = "COMPLAINT-DigitalEnvoyv.Google.pdf"
    # filename = "COMPLAINT-Waymo.pdf"
    # filename = "Tarantino_v_Gawker.pdf"
    # filename = "jl/Goddard Complaint.pdf"
    # filename = "musk v openai complaint.pdf"
    # filename = "HomeLight v. Shkipin_orphan_coa.pdf"  # header not all uppercase
    # filename = "Griffith_v_Tiktok.pdf"
    # filename = "McLaughlin v. HomeLIght - COMPLAINT.pdf"
    # filename = "Tires.Antitrust.Complaint.pdf"
    # filename = "Feldman Cross_Complaint_2_24_25.pdf"

 
    # complaint_text, case_number, is_image = get_text_from_pdf_fitz("backend/static/" + filename)
    
    # # complaint_text, case_number, _ = get_text_from_pdf_fitz("backend/static/good complaints/" +filename)
    # cover_page_text, complaint_body_text = complaint_text.split("<CoverPage>")
    # print("Cover_page: \n", cover_page_text)
    # text_by_lines = complaint_body_text.split('\n')
    # print("Total # lines: ", len(text_by_lines))
    # print("body lines begin:\n", text_by_lines[:10])
    
    # for line in text_by_lines[:50]:
    #     print(f"=>{line}<=")
 
    # hd,  line_numbers = extract_headings(text_by_lines)
    # for heading, line_num in zip(hd, line_numbers):
    #     print(f"{heading}\t(Line {line_num})")

    # hd_2_ln = get_headers_from_llm(text_by_lines)
    # print("hd_2_ln:\n", hd_2_ln)

    # para_2_ln = get_paralines_from_llm(text_by_lines)
    # if is_image:
    #     print("before longest sequence para_2_ln len = ", len(para_2_ln))
    #     para_key_lst = list(para_2_ln.keys())
    #     print("before longest inc sequence: ", para_key_lst)
    #     longest_inc_para_lst = longest_inc_sequence(para_key_lst)
    #     print("longest_inc_para_lst: ", longest_inc_para_lst)
    #     for each_para in para_key_lst:
    #         if each_para not in longest_inc_para_lst:
    #             print("deleting ", each_para)
    #             del para_2_ln[each_para]
    #     print("after longest sequence para_2_ln len = ", len(para_2_ln))

    # checked_para_2_ln = is_consecutive_or_expand(para_2_ln)
    # print(f"after is consecutive: # {len(checked_para_2_ln)}: {checked_para_2_ln}")


    # comand_prompt = open('backend/ai/prompts/answer/COMPRESS').read()
    # filled_in_prompt = comand_prompt + complaint_body_text
    # TOTAL_TOKENS = num_tokens_from_string(filled_in_prompt)
    # print("TOTAL # TOKENS = ", TOTAL_TOKENS)
    # summary1 =  chat_completion(filled_in_prompt)
    # print("summary1: \n", summary1)

    # start_time = time.time()
    # fa_text_lines = get_fa_text(complaint_body_text)
    # print("# FA lines: ", len(fa_text_lines))

    # fa_start_to_end_i = get_fa_loc(complaint_body_text)
    # print(fa_start_to_end_i)
    # for j in range(len(fa_start_to_end_i) - 1):
    #     start_line = text_by_lines[fa_start_to_end_i[j]:fa_start_to_end_i[j]+2] # this should be the heading or subheading line
    #     print(start_line)

    # # print("fa_text_lines start: ", fa_text_lines[:10])
    # # print("fa_text_lines ends: ", fa_text_lines[-10:])
   
    # # print("********")
    # fa_text_lines_chunks = split_into_chunks(fa_text_lines)
    # print("# number of fa chunks: ", len(fa_text_lines_chunks))
    # # for chunk in fa_text_lines_chunks:
    # #     print(' '.join(chunk))
    # #     print("********")

    # coa_text_lines = get_coa_text(complaint_text)
    # print("# number of coa chunks: ", len(coa_text_lines))
    # # for coa in coa_text_lines:
    # #     print(coa)
    # #     print("****")
    # end_time = time.time()
    # print(f"The function took {end_time - start_time} seconds to complete.")

    # plaintiff_l, defendant_l, venue = extract_parties_venue(' '.join(text_by_lines[:100]))
    # print("plaintiff: ", plaintiff_l)
    # print("# plaintiff = ", len(plaintiff_l))
    # print("defendant: ", defendant_l)
    # print("# defendant = ", len(defendant_l))
    # print("venue: ", venue)


# =============== RFP documents for testing  =================#
    # filename = "RFP (Uber).pdf"
#     filename = "Generated_RFP_Gawker.pdf"
    filename = "walmart rfp.pdf"
    # filename = "Bayside-RFP.pdf"

    rfp_text, case_number, _ = get_text_from_pdf_fitz("backend/static/" + filename)
    text_by_lines = rfp_text.split('\n')

    # def_textlines = get_rfp_section_text(rfp_text, section="definitions")
    # print("# definition blocks: ", len(def_textlines))
    # print("DEFINITIONS:")
    # print(def_textlines)

    # section_name = "Definition"
    # comand = open("backend/ai/prompts/rfp_response/LIST_DEF_INSTRUCT").read()
    # filled_in_prompt = comand.format(section=section_name, context='\n'.join(def_textlines))
    # # print("filled_in_prompt: \n", filled_in_prompt)
    # output = chat_completion_j(filled_in_prompt)
    # def_list = extract_from_json(f"{section_name}s", output)
    # print(f"# def_list: {len(def_list)}")
    # print("===")

    # instruct_textlines = get_rfp_section_text(rfp_text, section="instructions")
    # print("# instruction blocks: ", len(instruct_textlines))
    # print("INSTRUCTIONS: ")
    # print(instruct_textlines)
    
    # section_name = "Instruction"
    # comand = open("backend/ai/prompts/rfp_response/LIST_DEF_INSTRUCT").read()
    # filled_in_prompt = comand.format(section=section_name, context='\n'.join(instruct_textlines))
    # print("filled_in_prompt: \n", filled_in_prompt)
    # output = chat_completion_j(filled_in_prompt)
    # instruct_list = extract_from_json(f"{section_name}s", output)
    # print(f"# instruction_list: {len(instruct_list)}")
    # print("===")

    # headings, line_numbers = extract_headings(text_by_lines)
    # hd_lines = "\n".join(str(h) + "\t(" + str(l) + ")" for h, l in list(zip(headings, line_numbers)))
    # p = r'REQUEST\s+FOR\s+PRODUCTION\s+NO.\s*([0-9]+)|REQUEST\s+NO.\s*([0-9])+'
    # matches = re.findall(p, '\n'.join(headings))
    # items_dict = {}
    # if len(matches) > 5:
    #     # specific pattern for request item found, extract directly, if less then this, pass to llm will handle fine
    #     for i, h in enumerate(headings):
    #         match = re.search(p, h)
    #         if match:
    #             item_id = match.group(1) if match.group(1) is not None else match.group(2)
    #             print("id: ", item_id)
    #             item_ln = line_numbers[i]
    #             print("line number: ", item_ln)
    #             next_item_ln = line_numbers[i+1]
    #             print(text_by_lines[item_ln:next_item_ln])

    requests_start_end_i = get_rfp_section_text(text_by_lines, section="requests")
    requests_textlines = text_by_lines[requests_start_end_i[0]:requests_start_end_i[-1]]
    print("REQUESTS: ")
    print(requests_textlines)
    print("# request blocks: ", len(requests_textlines))
    print("===")

    before_requests_textlines = text_by_lines[:requests_start_end_i[0]]
    for section_name in ['definition', 'instruction']:
        print("section name: ", section_name)
        comand = open("backend/ai/prompts/rfp_response/IF_DEF_INST").read()
        filled_in_prompt = comand.format('\n'.join(before_requests_textlines))
        print("filled_in_prompt: \n", filled_in_prompt)
        output = chat_completion_j(filled_in_prompt)
        print("output: \n", output)

        # llm_list = extract_from_json(f"{section_name}s", output)
        # print(f"# {section_name}_list: {len(llm_list)}")
        # print(llm_list)
        # print("=====")
