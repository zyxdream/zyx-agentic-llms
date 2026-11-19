"""Role-based model policy for the Workspace agent.

One policy is resolved for a run and saved with the run. There is no user-facing
fast/deep mode. Each role names a provider, model and reasoning effort so a
provider can be introduced without changing the agent loop.
"""
import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_FILE = ROOT / 'config' / 'models.json'
POLICY_VERSION = 2
ROLES = (
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
LEGACY_SELECTORS = ('assistant_mode', 'model', 'selected_model')


def _policy_path():
    return Path(os.getenv('WORKSPACE_MODEL_POLICY_FILE', str(DEFAULT_POLICY_FILE)))


def _validate_role(role, value):
    if role not in ROLES:
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


def load_model_policy():
    """Load defaults from config/models.json, then apply per-role environment overrides."""
    path = _policy_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f'Model policy file not found: {path}') from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f'Model policy file is not valid JSON: {path}') from exc
    if not isinstance(raw, dict) or not isinstance(raw.get('roles'), dict):
        raise ValueError('Model policy must contain a roles object.')
    default_effort = raw.get('default_effort', 'medium')
    roles = {}
    for role in ROLES:
        value = raw['roles'].get(role)
        if value is None:
            raise ValueError(f'Model policy is missing role: {role}')
        if isinstance(value, str):
            value = {'model': value}
        if not isinstance(value, dict):
            raise ValueError(f'Model role {role} must be a string or object.')
        merged = {'provider': value.get('provider', 'openai'),
                  'model': value.get('model'),
                  'effort': value.get('effort', default_effort)}
        override = os.getenv(f'WORKSPACE_MODEL_{role.upper()}')
        if override:
            if ':' in override:
                merged['provider'], merged['model'] = override.split(':', 1)
            else:
                merged['model'] = override
        effort_override = os.getenv(f'WORKSPACE_EFFORT_{role.upper()}')
        if effort_override:
            merged['effort'] = effort_override
        roles[role] = _validate_role(role, merged)
    return {'policy_version': raw.get('policy_version', POLICY_VERSION), 'roles': roles}


def _legacy_mode(data):
    if not isinstance(data, dict):
        return 'deep'
    if 'assistant_mode' in data:
        mode = data['assistant_mode']
        if mode not in {'fast', 'deep'}:
            raise ValueError('Workspace model configuration is invalid.')
        return mode
    legacy_model = data.get('model') or data.get('selected_model')
    return 'fast' if legacy_model in {'gpt-6-sol', 'gpt-6.1-sol'} else 'deep'


def legacy_policy(data):
    """Read-only policy for runs admitted before role-based configuration.

    New runs never write assistant_mode. Old rows keep their historical behavior
    so retries and saved-run inspection remain reproducible.
    """
    mode = _legacy_mode(data)
    if mode == 'fast':
        roles = {role: {'provider': 'openai', 'model': 'gpt-6-luna', 'effort': 'medium'}
                 for role in ROLES}
    else:
        roles = {role: {'provider': 'openai', 'model': 'gpt-6.1-sol', 'effort': 'high'}
                 for role in ROLES}
        roles['preparation'] = {'provider': 'openai', 'model': 'gpt-6-luna', 'effort': 'medium'}
        roles['utility'] = {'provider': 'openai', 'model': 'gpt-6.1-sol', 'effort': 'medium'}
    return {'policy_version': 1, 'legacy_mode': mode, 'roles': roles}


def _saved_policy(data):
    if not isinstance(data, dict):
        return None
    value = data.get('model_policy')
    if not isinstance(value, dict) or not isinstance(value.get('roles'), dict):
        return None
    roles = {}
    for role in ROLES:
        item = value['roles'].get(role)
        if item is None:
            return None
        roles[role] = _validate_role(role, item)
    return {'policy_version': value.get('policy_version', POLICY_VERSION), 'roles': roles}


def resolve_role(data, role):
    """Return the provider/model/effort for one role in this run."""
    if role not in ROLES:
        raise ValueError(f'Unknown model role: {role}')
    policy = _saved_policy(data) or legacy_policy(data)
    return copy.deepcopy(policy['roles'][role])


def default_role(role):
    """Return the current configured default for a role outside a saved run."""
    if role not in ROLES:
        raise ValueError(f'Unknown model role: {role}')
    return copy.deepcopy(load_model_policy()['roles'][role])


def resolve_policy(data):
    """Return the complete policy used by this run."""
    return _saved_policy(data) or legacy_policy(data)


def apply_model_policy(data, previous=None):
    """Attach one resolved policy to a new run, or retain the saved policy.

    Explicit legacy selectors are accepted for older clients and are left in
    place; they resolve through legacy_policy(). New browser requests carry no
    selectors and receive the current role policy snapshot.
    """
    result = dict(data)
    if previous is not None:
        saved = _saved_policy(previous)
        incoming = _saved_policy(result)
        if saved is not None:
            if incoming is not None and incoming != saved:
                raise ValueError('The submitted prompt identifier is already in use')
            result['model_policy'] = saved
            return result
        for key in LEGACY_SELECTORS:
            if key in previous:
                result[key] = previous[key]
        return result
    if _saved_policy(result) is not None:
        result['model_policy'] = _saved_policy(result)
        return result
    if any(key in result for key in LEGACY_SELECTORS):
        return result
    result['model_policy'] = load_model_policy()
    return result
