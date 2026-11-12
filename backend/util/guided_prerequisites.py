import json


def _has_meaningful_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return False
        if stripped in ('[]', '{}', 'null'):
            return False
        try:
            parsed = json.loads(stripped)
        except Exception:
            return True
        return _has_meaningful_value(parsed)
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def guided_prerequisite_error(memory, required_keys, workflow_name):
    missing = [key for key in required_keys if not _has_meaningful_value(memory.get(key, ''))]
    if not missing:
        return None
    return {
        'msg': f'Please finish preparing {workflow_name} before continuing.',
        'error_message': f'Please finish preparing {workflow_name} before continuing.',
        'missing_prerequisites': missing,
    }
