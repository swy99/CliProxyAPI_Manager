# Changelog

## 1.2.0 - 2026-08-12

- Added an optional post-install step that offers to add Claude Code shell shortcuts and ensure auto-compaction is enabled.
- The step prompts once during install (default No) and is skipped in non-interactive runs.
- Adds `cs`/`csr`/`csw` and GPT-backend-safe `csg`/`csgr`/`csgw` to the Windows PowerShell profile and cmd (doskey via user AutoRun), and sets `autoCompactEnabled` in `settings.json`. Existing files are preserved and updates are idempotent.

## 1.1.0 - 2026-08-11

- Added a verified PowerShell installer for CLIProxyAPI, Claude Code, and Manager.
- Added an npm/npx wrapper for the Windows installer.
- Added tag-based release automation for the Manager binary and checksums.

## 1.0.0 - 2026-07-31

- Added a Windows tray interface for CLIProxyAPI status and authentication.
- Added process supervision with restart backoff and HTTP health checks.
- Added OAuth expiry notifications and provider-specific login shortcuts.
- Added verified CLIProxyAPI updates with backup and rollback.
- Added per-user Windows startup registration.
