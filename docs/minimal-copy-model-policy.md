# Minimal copy and backend-owned model defaults

Empty conversations now display “What’s next?” and the composer uses “Message Workspace…” with the accessible label “Message”. The eyebrow and supporting task-list paragraph have been removed. The replacement phrases were compared with the retired frontend and had no matches; no comparison checkout is needed by the application or test suite.

The frontend has no model/reasoning selector, mode state, or model field in its submission fingerprint. Browser requests omit `assistant_mode`, `model` and `selected_model`. Functional Sources/Document controls and the existing attachment, Send, Stop, clarification and retry behavior remain.

## Backend compatibility

- `WORKSPACE_DEFAULT_MODE` is a private backend setting, defaulting to `fast` to preserve the former frontend default and its existing model/effort mapping. It is validated at startup and is not exported in public frontend configuration.
- Newly admitted requests without model selectors save this server choice in their run data before dispatch.
- HTTP resubmissions with the same run ID inherit the saved selector fields instead of consulting a newer default. Existing query/source identity checks still reject changed requests.
- Legacy explicit selectors are retained for older clients. Historical rows without any selector still resolve according to their previous behavior; they are not stamped with the new default. Explicit retries likewise retain the saved run data.
- Configuration errors no longer ask users to choose Fast/Deep. The existing model identifiers, provider transport and helper mappings have not been changed.

No database migration, existing-row rewrite, new provider selection, model-setting consolidation or automatic routing is included. Deciding future models remains separate backend work.

## Verification

All **496 backend tests**, **96 frontend component tests**, and **78 browser workflows** passed. The full backend run includes actual isolated Redis/Celery replays with mode-less requests and Docker execution; the new policy cases cover the current default, a changed server default, lost-response resubmission, explicit retry, legacy selectors, historical mode-less rows, policy conflicts and generic configuration errors. The production build passed and the minimal welcome was visually inspected.

Workspace was refreshed on frontend 3010/backend 5010/Redis 6390 after confirming no active work. Every existing application table/account hash was unchanged, and the other application's listener PIDs were unchanged. Verification made no paid-provider requests. Reports are under ignored `artifacts/minimal-copy/` and `artifacts/workspace-replay/`. No pushes were made.
