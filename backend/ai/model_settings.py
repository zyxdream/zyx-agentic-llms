"""Role-based model settings for Workspace.

Where model choices live:
- Defaults: ``config/models.json``. This is the only committed table of
  provider/model/effort choices.
- Optional overrides: ``WORKSPACE_MODEL_<ROLE>`` and
  ``WORKSPACE_EFFORT_<ROLE>`` in the private ``.env``.
- Per-run copy: ``WorkspaceRun.data_json["model_settings"]``. A run freezes its
  resolved table so retries do not change models when the defaults change.

There is no fast/deep mode. Every caller asks for a role by name.
"""
import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL_SETTINGS_FILE = ROOT / 'config' / 'models.json'
MODEL_SETTINGS_VERSION = 2
MODEL_ROLES = (
    'preparation',
    'controller',
    'analysis',
    'writer',
    'verifier',
    'research',
    'search',
    'utility',
    'court_format',
)
SUPPORTED_PROVIDERS = {'openai'}
SUPPORTED_EFFORTS = {'none', 'low', 'medium', 'high', 'max'}
# Obsolete request fields from the retired fast/deep UI. They are ignored and
# stripped from new run data; historical rows may still contain them.
OBSOLETE_SELECTOR_KEYS = ('assistant_mode', 'model', 'selected_model')


def _settings_path():
    return Path(os.getenv('WORKSPACE_MODEL_SETTINGS_FILE', str(MODEL_SETTINGS_FILE)))


def _validated_model_setting(role, value):
    """Validate one role's provider/model/effort and return a clean copy."""
    if role not in MODEL_ROLES:
        raise ValueError(f'Unknown model role: {role}')
    if not isinstance(value, dict):
        raise ValueError(f'Model role {role} must be an object.')
    provider = value.get('provider', 'openai')
    model = value.get('model')
    effort = value.get('effort', 'medium')
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f'Unsupported model provider for {role}: {provider}')
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f'Model role {role} requires a model identifier.')
    if effort not in SUPPORTED_EFFORTS:
        raise ValueError(f'Unsupported reasoning effort for {role}: {effort}')
    return {'provider': provider, 'model': model.strip(), 'effort': effort}


def load_model_settings():
    """Load the role table from config/models.json and apply env overrides.

    The environment overrides are optional. They exist so a deployment can
    change one role without editing the committed defaults.
    """
    path = _settings_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f'Model settings file not found: {path}') from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f'Model settings file is not valid JSON: {path}') from exc
    if not isinstance(raw, dict) or not isinstance(raw.get('roles'), dict):
        raise ValueError('Model settings must contain a roles object.')
    default_effort = raw.get('default_effort', 'medium')
    roles = {}
    for role in MODEL_ROLES:
        value = raw['roles'].get(role)
        if value is None:
            raise ValueError(f'Model settings are missing role: {role}')
        if isinstance(value, str):
            value = {'model': value}
        if not isinstance(value, dict):
            raise ValueError(f'Model role {role} must be a string or object.')
        merged = {'provider': value.get('provider', 'openai'),
                  'model': value.get('model'),
                  'effort': value.get('effort', default_effort)}
        model_override = os.getenv(f'WORKSPACE_MODEL_{role.upper()}')
        if model_override:
            if ':' in model_override:
                merged['provider'], merged['model'] = model_override.split(':', 1)
            else:
                merged['model'] = model_override
        effort_override = os.getenv(f'WORKSPACE_EFFORT_{role.upper()}')
        if effort_override:
            merged['effort'] = effort_override
        roles[role] = _validated_model_setting(role, merged)
    return {'model_settings_version': raw.get('model_settings_version', MODEL_SETTINGS_VERSION),
            'roles': roles}


def default_model_setting(role):
    """Return the current configured provider/model/effort for one role."""
    if role not in MODEL_ROLES:
        raise ValueError(f'Unknown model role: {role}')
    return copy.deepcopy(load_model_settings()['roles'][role])


def _saved_model_settings(data):
    """Return a validated per-run snapshot, or None for a historical row."""
    if not isinstance(data, dict):
        return None
    value = data.get('model_settings')
    if not isinstance(value, dict) or not isinstance(value.get('roles'), dict):
        return None
    roles = {}
    for role in MODEL_ROLES:
        item = value['roles'].get(role)
        if item is None:
            return None
        roles[role] = _validated_model_setting(role, item)
    return {'model_settings_version': value.get('model_settings_version', MODEL_SETTINGS_VERSION),
            'roles': roles}


def run_model_setting(data, role):
    """Return one role's settings for a run, falling back to current defaults.

    Historical rows without a saved snapshot resolve the current table. Their
    obsolete assistant_mode/model fields are ignored.
    """
    if role not in MODEL_ROLES:
        raise ValueError(f'Unknown model role: {role}')
    settings = _saved_model_settings(data) or load_model_settings()
    return copy.deepcopy(settings['roles'][role])


def run_model_settings(data):
    """Return the complete role table for a run, falling back to current defaults."""
    return _saved_model_settings(data) or load_model_settings()


def attach_model_settings(data, previous=None):
    """Freeze the current role table into a new run, or keep the saved table.

    New runs receive the current table. Existing runs with a saved snapshot keep
    it. Historical rows without a snapshot receive the current table; their
    obsolete selector fields are ignored.
    """
    result = dict(data)
    for key in OBSOLETE_SELECTOR_KEYS:
        result.pop(key, None)
    incoming = _saved_model_settings(result)
    if previous is not None:
        saved = _saved_model_settings(previous)
        if saved is not None:
            if incoming is not None and incoming != saved:
                raise ValueError('The submitted prompt identifier is already in use')
            result['model_settings'] = saved
            return result
    if incoming is not None:
        result['model_settings'] = incoming
        return result
    result['model_settings'] = load_model_settings()
    return result
