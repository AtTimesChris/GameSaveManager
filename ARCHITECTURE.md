# Architecture and Behaviour Baseline — Game Save Manager

**Purpose:** Record documented system responsibilities and provide an architecture-discovery checklist. This is not proof that every capability exists in the current source. `AGENTS.md` governs safety and approval; the actual implementation is the evidence of current behaviour.

## 1. Documented system overview

Game Save Manager is a Python application intended to manage game-save locations and backups across Windows 11 and Steam Deck (SteamOS/Linux). Documented responsibilities include:
- Game configuration and metadata.
- Platform-specific save paths.
- Backup and historical save versions.
- Synchronisation through a NAS, with local fallback.
- Conflict detection and resolution.
- Restore operations.
- Steam launch hooks.
- Optional background backup.
- Logging, diagnostics, and upgrade packaging.

Main documented files include `savemanager.py` (application), `Games.json` (game configuration), and `Backups/` (backup/upgrade history). Verify actual filenames, responsibilities, and layout before relying on them.

## 2. Platform boundaries

### Windows
- Windows filesystem semantics and environment variables.
- Windows 11 desktop runtime.
- Steam non-Steam shortcut integration.
- NAS via configured network paths or mapped locations.

### Steam Deck / SteamOS
- Linux filesystem semantics and Python runtime.
- Steam launch options and shell scripts.
- Mounted NAS locations and local fallback.

Shared code must not assume path syntax, process launch behaviour, permissions, or mounted storage are identical across platforms.

## 3. Configuration boundary

`Games.json` is the documented game configuration. Discover the current schema from the parser, writer, examples, and migration logic.

Constraints:
- Preserve existing keys and unknown fields where current behaviour supports them.
- Preserve legacy formats and migration behaviour.
- Preserve platform-specific path behaviour and precedence.
- Never test against the live configuration.
- Use sanitised fixtures with paths redirected to isolated test folders.
- Do not infer schema from old examples alone.

## 4. Save and backup data flow — intended model only

Historical documentation describes a flow that may include:
1. Resolve the configured save path for the current platform.
2. Check source and destination availability.
3. Apply configured include/exclude filters.
4. Back up save files and record file-level outcomes.
5. Preserve historical versions and state metadata.
6. Synchronise through NAS when available.
7. Use configured local fallback when NAS is unavailable.
8. Reconcile when NAS access returns, respecting conflict policy.
9. Restore only after preserving a recoverable pre-restore state.

Confirm the actual sequence, atomicity, metadata names, destination layout, and failure handling from code before modifying anything.

## 5. Conflict policy concepts

Historical policy names include `Ask`, `AutoNewest`, and `AlwaysLocal`.
- `Ask`: intended to surface conflicts for user decision.
- `AutoNewest`: intended to select according to established comparison rules.
- `AlwaysLocal`: intended to retain local state and report remote changes.

These are documented concepts, not guaranteed current enum values or exact semantics. Verify the implementation. Avoid silent overwrite, preserve competing versions where required, and do not treat matching filenames or timestamps alone as proof of identical/current content.

## 6. Emulator-specific considerations

Some emulator configurations may use shared memory-card files across multiple games. Do not treat such files as independent per-game save files without evidence. Preserve complete historical snapshots and avoid automatic merging of shared memory cards.

## 7. Steam integration boundary

Historical requirements include a Steam Deck wrapper preserving `%command%` and a Windows-specific non-Steam launch approach. These examples are not guaranteed current behaviour.

Before changing integration:
- Inspect launch scripts and argument parsing.
- Trace preservation/execution of original game commands.
- Check quoting and paths containing spaces.
- Test with a dummy executable and isolated Steam configuration.
- Never edit real Steam shortcut/library files without explicit approval.

## 8. Background service boundary

Background services, scheduled tasks, game-exit monitors, and launch hooks may run independently and write data. Treat installation, registration, start/stop, and configuration changes as protected external side effects requiring explicit approval. Use mocks or isolated test processes.

## 9. Logging and diagnostics

Historical log paths include `Logs/savemanager.log` and `Logs/crash.log`. Verify actual paths and behaviour. Keep test logs inside the testing workspace. Never expose secrets or unnecessarily sensitive paths in logs/reports.

## 10. Upgrade boundary

The historical full specification documents `GameSaveManager-v{version}.zip`, a destination under `Backups/Previous Versions/<version>/`, and a top-level `CHANGELOG.md` plus `ModifiedFiles/`. Verify the actual installer before preparing a package. Never apply a package automatically.

## 11. Architecture discovery checklist

Before a code change, inspect and record relevant items:
- Entry points and command-line arguments.
- Menu structure and navigation.
- Main modules/functions and call graph around the target.
- Configuration schema, parser, writer, and migration paths.
- Path resolution and platform detection.
- Backup/restore/synchronisation sequence.
- Conflict policy implementation.
- NAS and local fallback behaviour.
- Steam integration.
- Background tasks and lifecycle.
- Logging and exception handling.
- Upgrade installer and package validation.
- Existing tests and fixtures.

Do not update this document with unverified assumptions. If architecture differs from this document, report the discrepancy and propose a documentation update rather than silently rewriting the implementation to match historical notes.
