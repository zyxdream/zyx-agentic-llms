# Model settings

Workspace uses one role-based model table. There is no fast/deep mode and no
frontend model selector.

## Where model choices are set

There are exactly three layers:

| Layer | Location | Purpose |
| --- | --- | --- |
| Committed defaults | `config/models.json` | The only place the actual role → model choices are defined |
| Optional overrides | `WORKSPACE_MODEL_<ROLE>`, `WORKSPACE_EFFORT_<ROLE>` in `.env` | Change one role without editing the committed defaults |
| Per-run snapshot | `WorkspaceRun.data_json["model_settings"]` | Freeze the resolved table for retries and audit |

Edit `config/models.json` to change the default model for a role. Use the
environment overrides only when a deployment needs a different value.

## Defaults

| Role | Model | Effort | Used for |
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

## Optional overrides

These are optional. They exist so a deployment can change one role without
editing the committed defaults.

```dotenv
WORKSPACE_MODEL_CONTROLLER=openai:gpt-6.1-sol
WORKSPACE_MODEL_PREPARATION=openai:gpt-6-luna
WORKSPACE_EFFORT_VERIFIER=high
```

The value may be `provider:model` or just `model` (provider defaults to
`openai`). Invalid providers, models or efforts fail at settings load rather than
silently falling back.

## Per-run snapshot

At admission, `attach_model_settings()` resolves the table and saves it in the
run's `data_json` as `model_settings`. Resubmissions, retries and saved-run
inspection reuse that snapshot even if `config/models.json` or the environment
changes later.

The browser sends no model fields. The frontend has no model/reasoning selector,
mode state, or model field in its submission fingerprint.

## Historical rows

Runs admitted before role settings may contain obsolete `assistant_mode`,
`model` or `selected_model` fields. Those fields are historical metadata and are
ignored. A run without a `model_settings` snapshot resolves the current table.

New runs never write the obsolete selector fields. No fast/deep branch exists.

## Changing a model

1. Edit `config/models.json`, or set the role override in `.env`.
2. Restart the backend and workers.
3. New runs use the new table. Existing runs keep their saved snapshot.

## Code map

| Concern | Location |
| --- | --- |
| Load defaults and overrides | `backend/ai/model_settings.py` → `load_model_settings()` |
| Current default for one role | `default_model_setting(role)` |
| One role for a run | `run_model_setting(data, role)` |
| All roles for a run | `run_model_settings(data)` |
| Freeze settings into a run | `attach_model_settings(data, previous)` |
| Default table | `config/models.json` |
