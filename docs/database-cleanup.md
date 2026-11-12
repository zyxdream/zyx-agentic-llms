# Database and Word add-in audit

The active database is `instance/zyx-agentic-llms.db`. The November 12, 2026 audit found exactly one owner account with a local password hash and no external identity value. No legacy branding or Word add-in values were present in any stored field.

## Cleanup applied

- Added forward migration `0003_local_accounts_only` to remove the unused external identity column from `user`. Historical migrations remain intact; upgrading to the current head produces a clean schema.
- Verified that the existing user ID, email, password hash, bootstrap record and every retained application row were unchanged after migration. No account was recreated, and the session signing key was unchanged.
- Backed up the active database before migration and archived five unused copied database files, an obsolete private configuration backup and two empty add-in directories outside this project. None of those files were in use. The private archive location is recorded in ignored `artifacts/database-cleanup/verification.json`.
- Removed the old brand from the research HTTP client identity and remaining example/fixture references.
- Confirmed that Word add-in frontend/backend code, Office manifests and application routes are absent. It was the integration that exposed document assistance inside Microsoft Word's task pane. Normal Workspace DOCX downloads and their XML text sanitization remain supported.

## Verification

The final audit scanned all active SQLite schema objects and every stored text field for retired brand names, external user-pool identifiers and Word add-in references. There were no matches. Database integrity and foreign-key checks passed.

All **71 focused tests passed**, covering schema upgrade/downgrade, existing-account/data/session preservation, local login and CSRF, resource isolation, Workspace APIs, absent add-in routes, research retrieval, and document exports including actual Docker-based PDF conversion.

Workspace was restarted successfully on frontend 3010/backend 5010/Redis 6390. Other application listener PIDs were unchanged. No provider calls, commits or pushes were made.
