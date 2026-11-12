"""Opt-in local evidence for SDK structured-response validation failures."""
import json
import logging
import os
from pathlib import Path
import traceback
import uuid
from datetime import datetime, timezone

from pydantic import ValidationError


logger = logging.getLogger(__name__)


def _capture(directory, raw, error, request_kwargs):
    """Capture response evidence without changing how the original error exits."""
    try:
        from backend.util.workspace_service import current_execution
        response = raw.http_response
        body = response.content
        try:
            envelope = json.loads(body)
        except (ValueError, UnicodeError):
            envelope = {}
        schema = request_kwargs['text_format']
        execution = current_execution() or {}
        metadata = {
            'captured_at': datetime.now(timezone.utc).isoformat(),
            'workflow_id': execution.get('id'),
            'model': request_kwargs.get('model'),
            'reasoning': request_kwargs.get('reasoning'),
            'schema_name': schema.__name__,
            'response_schema': schema.model_json_schema(),
            'http_status': response.status_code,
            'request_id': response.headers.get('x-request-id'),
            'response_id': envelope.get('id'),
            'response_status': envelope.get('status'),
            'incomplete_details': envelope.get('incomplete_details'),
            'usage': envelope.get('usage'),
            'exception_type': type(error).__name__,
            'exception_message': str(error),
            'errors': error.errors(include_input=False, include_url=False),
            'traceback': ''.join(traceback.format_exception(type(error), error, error.__traceback__)),
        }
        root = Path(directory).resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = root / uuid.uuid4().hex
        destination.mkdir(mode=0o700)
        for filename, content in (
            ('response-body.json', body),
            ('diagnostic.json', json.dumps(metadata, ensure_ascii=False, indent=2, default=str).encode('utf-8')),
        ):
            descriptor = os.open(destination / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
        logger.warning('Structured response diagnostic saved: %s', destination)
    except Exception as capture_error:
        logger.warning('Structured response diagnostic capture failed: %s', type(capture_error).__name__)


def parse_response(client, request_kwargs):
    directory = os.environ.get('LLM_PARSE_DIAGNOSTICS_DIR')
    if not directory:
        return client.responses.parse(**request_kwargs)
    # The same SDK parser runs below; retaining the raw response makes its body
    # available if parsing raises. Request content and generation options match.
    raw = client.responses.with_raw_response.parse(**request_kwargs)
    try:
        return raw.parse()
    except ValidationError as error:
        _capture(directory, raw, error, request_kwargs)
        raise
