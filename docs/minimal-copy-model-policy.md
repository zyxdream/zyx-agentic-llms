# Model policy

Workspace uses one role-based model policy per run. There is no user-facing
fast/deep mode and no frontend model selector.

## Roles and defaults

Defaults live in `config/models.json`:

| Role | Default model | Effort | Used for |
| --- | --- | --- | --- |
| `preparation` | `gpt-6-luna` | medium | Uploaded-document summaries and navigation records |
| `controller` | `gpt-6.1-sol` | medium | Next-action and tool-choice decisions |
| `analysis` | `gpt-6.1-sol` | medium | `analyze_material` findings |
| `writer` | `gpt-6.1-sol` | medium | `create_draft` and `revise_draft` |
| `verifier` | `gpt-6.1-sol` | medium | `verify_draft` findings |
| `research` | `gpt-6-luna` | medium | Authority discovery and legal-source search |
| `search` | `gpt-6-luna` | medium | Standalone web-search synthesis |
| `utility` | `gpt-6-luna` | medium | Compaction, conversation brief, editor-write check |
| `court_format` | `gpt-6-luna` | medium | Pleading format resolution |

The rule is deliberate: luna for high-volume or low-logic work, `gpt-6.1-sol`
medium for logic-heavy and accuracy-critical work.

## Per-run snapshot

At admission, the resolved policy is saved in the run's `data_json` as
`model_policy`. Resubmissions, retries and saved-run inspection reuse that
snapshot even if `config/models.json` or the environment changes later.

The browser sends no model fields. The frontend has no model/reasoning selector,
mode state, or model field in its submission fingerprint.

## Overrides

Per-role environment overrides are optional:

```dotenv
WORKSPACE_MODEL_CONTROLLER=openai:gpt-6.1-sol
WORKSPACE_MODEL_PREPARATION=openai:gpt-6-luna
WORKSPACE_EFFORT_VERIFIER=high
```

The value may be `provider:model` or just `model` (provider defaults to
`openai`). Invalid providers, models or efforts fail at policy load rather than
silently falling back.

## Legacy runs

Runs admitted before the role policy keep their historical behavior:

- `assistant_mode=fast` resolves to luna medium for all roles.
- `assistant_mode=deep` resolves to `gpt-6.1-sol` high for controller/tools,
  `gpt-6.1-sol` medium for utility, and luna medium for preparation.
- Historical rows with no selector resolve as deep.
- Explicit legacy `model`/`selected_model` selectors are retained for old
  clients and are not rewritten.

New runs never write `assistant_mode`, `model` or `selected_model`.

## Historical note

This replaces the earlier minimal-copy policy where `WORKSPACE_DEFAULT_MODE`
chose between `WORKSPACE_FAST_MODEL` and `WORKSPACE_DEEP_MODEL`. Those settings
are removed from `.env.example`; the legacy read-only resolver remains so
existing rows and retries stay reproducible. No database migration is needed.
