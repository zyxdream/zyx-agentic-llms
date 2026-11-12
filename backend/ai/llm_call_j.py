# 
"""
Stand-alone LLM calls for parallel processing with the Subprocessing module.
"""
import json
import sys

from backend.ai.base import chat_completion_j, chat_completion_parse
from backend.ai import base as ai_base
from backend.util.analytics import capture_guided_llm_usage


def _completion(prompt: str, schema_name: str | None):
    if schema_name is None:
        return chat_completion_j(prompt=prompt)

    from backend.ai.complaint_schemas import COMPLAINT_STEP0_SCHEMAS

    schema = COMPLAINT_STEP0_SCHEMAS.get(schema_name)
    if schema is None:
        raise ValueError(f"Unknown LLM output schema: {schema_name}")
    parsed = chat_completion_parse(
        prompt,
        schema,
        model=ai_base.DEFAULT_MODEL,
        reasoning_effort="high",
    )
    return parsed.model_dump(by_alias=True) if parsed is not None else None


if __name__ == "__main__":
    args = sys.argv[1:]
    analytics_envelope = bool(args and args[0] == "--analytics-envelope")
    if analytics_envelope:
        args = args[1:]

    schema_name = None
    if args and args[0] == "--schema":
        if len(args) < 3:
            sys.exit(1)
        schema_name = args[1]
        args = args[2:]

    if not args:
        sys.exit(1)

    if analytics_envelope:
        with capture_guided_llm_usage() as usage_events:
            llm_output = _completion(args[0], schema_name)
        print(json.dumps({"output": llm_output, "usage": usage_events}))
    else:
        llm_output = _completion(args[0], schema_name)
        print(llm_output)
