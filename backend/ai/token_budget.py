"""Preflight textual Responses requests before provider dispatch."""
import json
import tiktoken

CONTEXT_LIMITS = {name: 1_050_000 for name in ('gpt-5.5', 'gpt-5.4', 'gpt-6-sol', 'gpt-6.1-sol', 'gpt-6-luna')}
CONTEXT_LIMITS['gpt-5.4-mini'] = 400_000
SAFETY_MARGIN = 2_048
DEFAULT_OUTPUT_RESERVE = 128_000


class ContextBudgetExceeded(ValueError):
    """A request does not fit; callers may reduce supporting evidence and retry.

    The provider adapter cannot know which text is editable source. Drafting
    preflight can set can_reduce_evidence=False when the exact draft, question
    and instructions already exceed the budget without any supporting evidence.
    """

    def __init__(self, message, *, can_reduce_evidence=True, tokens=None, budget=None):
        super().__init__(message)
        self.can_reduce_evidence = can_reduce_evidence
        # Exact preflight estimate and allowance when the provider adapter
        # measured the request. Other call sites may not have that information.
        self.tokens = tokens
        self.budget = budget


class IncompleteStructuredOutput(ValueError):
    pass


def usable_input_budget(request):
    """Input allowance after the existing output reserve and safety margin."""
    limit = CONTEXT_LIMITS.get(request.get('model'))
    if limit is None:
        return  # Other products may register providers with their own budgets.
    reserve = request.get('max_output_tokens') or DEFAULT_OUTPUT_RESERVE
    return limit - reserve - SAFETY_MARGIN


def estimate_text_request(request):
    """Use the same wire-text estimate for preflight and agent compaction.

    Encrypted continuation items are counted as serialized text, as before;
    this is an application estimate rather than the provider's token count.
    """
    messages = request.get('input', [])
    if not isinstance(messages, (str, list)):
        return
    # Textual tool histories contain reasoning/function-call items, not just
    # messages. Count the complete wire input and tool schemas, never skip their
    # preflight. Actual image/audio inputs still need a modality estimator.
    if isinstance(messages, list) and any(
        not isinstance(m, dict) or (isinstance(m.get('content'), list) and any(
            part.get('type') not in ('input_text', 'output_text') for part in m['content']))
        for m in messages
    ):
        return
    schema = request.get('text_format')
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False) if schema else ''
    material = json.dumps(messages, ensure_ascii=False) + schema_json + json.dumps(request.get('tools', []), ensure_ascii=False)
    return len(tiktoken.get_encoding('cl100k_base').encode(material)) + 256


def estimate_text_tokens(value):
    """Estimate one serialized value with the same application-side encoding."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(tiktoken.get_encoding('cl100k_base').encode(text))


def validate_text_request(request):
    budget = usable_input_budget(request)
    if budget is None:
        return
    tokens = estimate_text_request(request)
    if tokens is not None and tokens > budget:
        raise ContextBudgetExceeded(
            'The complete request exceeds the model context budget; process source material in bounded batches.',
            tokens=tokens, budget=budget)
    return tokens
