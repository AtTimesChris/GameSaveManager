# AGENTS.md — Game Save Manager

**Purpose:** Authoritative, tool-independent instructions for every AI coding agent, including LM Studio Bionic and any connected coding agent.

**Project:** Game Save Manager  
**Documented application version:** 14.5.1 is a historical documentation baseline only. The supplied project tree also showed a `v14.5.2_20261108-210400` snapshot; neither establishes the current root application's version. Verify the actual source before relying on a version.  
**Primary platforms:** Windows 11 and Steam Deck (SteamOS/Linux)  
**Main application:** Python (`savemanager.py`)  
**Configuration:** JSON (`Games.json`)  
**Dependency policy:** Prefer the Python standard library; obtain approval before adding dependencies.

---

# 1. Absolute priorities

These priorities are ordered. A lower priority must never override a higher one.

1. **Preserve all existing functionality.** Do not remove, weaken, bypass, replace, or silently alter working behaviour. Improvements should be additive or narrowly scoped corrections unless the user explicitly approves a behaviour change.
2. **Protect the live project and user data.** Never experiment on the live project, installed application, real save folders, production configuration, NAS backup, Steam configuration, or existing backup history.
3. **Keep the live project read-only.** The live project is the complete `v14 (Bionic)` project folder identified by the user, including source, configuration, logs, state, backups, historical versions, and all other contents. It is not just the Python source files.
4. **Use a separate testing copy.** Inspect and copy the necessary live project files into a separate testing workspace. All implementation and testing take place in that workspace, never in the live project.
5. **Require explicit approval for live changes.** Any action that changes the live project or production data—including promoting staged files, editing, overwriting, creating, deleting, moving, renaming, formatting, patching, applying an upgrade, or changing live configuration—requires explicit approval before it happens. Approval is limited to the exact paths and actions agreed for that user prompt.
6. **Make the smallest safe change.** Investigate first, preserve interfaces and compatibility, and avoid unrelated refactoring.
7. **Verify and report honestly.** Never claim that something works unless the relevant test was actually performed. When a test finds an error, investigate and fix it in the testing copy, then rerun relevant tests until they pass or a genuine blocker is established. Never conceal failures or imply that untested areas passed.

If an instruction, convenience, tool default, or agent workflow conflicts with these priorities, follow these priorities and stop to ask the user if necessary.

# 2. Permission model

## 2.1 Actions permitted without a separate approval request

The user's request to work on a task authorises the agent to perform ordinary work **inside the separate testing workspace**, within the scope of that task. The agent may:
- Read and inspect the live project without changing it.
- Copy the minimum necessary project source, scripts, and documentation from the live project into the separate testing workspace.
- Create or refresh the testing copy from the live project, provided the live source remains untouched and production data is excluded.
- Create, edit, replace, or delete files inside the testing workspace as reasonably required for the requested task.
- Create synthetic test fixtures, isolated test configuration, temporary output, test logs, and evidence inside the testing workspace.
- Run tests and application checks when their inputs and all possible writes are demonstrably confined to the testing workspace.
- Fix errors discovered during testing and rerun the relevant tests, without asking for approval for every test-copy edit.

These permissions do not authorise unrelated feature work, external side effects, dependency installation, system changes, or live-project changes.

## 2.2 Actions that always require explicit approval

Obtain explicit approval before:
- Editing, creating, deleting, moving, renaming, formatting, overwriting, patching, or applying anything in the live `v14 (Bionic)` project.
- Copying staged files back into the live project or applying an upgrade to it.
- Changing real `Games.json`, real save folders, production backups, NAS content, Steam userdata/shortcuts, installed application files, or live state/configuration.
- Installing/updating dependencies, tools, or system packages.
- Changing environment variables, registry entries, services, scheduled tasks, system configuration, or persistent Steam configuration.
- Changing Git state (branches, commits, tags, stashes, resets, rebases, cherry-picks, pushes, or destructive checkout/clean operations).
- Running a command whose side effects cannot be established or whose writes may escape the testing workspace.
- Sending project files, logs, configuration, or user data to external services or cloud models.

A test command that writes only to the isolated testing workspace is permitted as part of the requested task. A command that may write outside it is not.

## 2.3 Scope of live-change approval

Every live change requires a proposal that identifies the exact live paths, operations, reasons, functional impact, backup/rollback plan, and verification plan. Ask one clear approval question and wait. Silence, vague acknowledgement, or approval of a different task is not approval.

Approval applies only to the exact live actions and paths described and only to the current user prompt/task. It expires when that task/prompt is complete. A later prompt requires fresh approval, even if it concerns the same files. If the implementation needs an additional path or materially different operation, stop and request approval again.

A user request to fix or improve something authorises work in the testing copy; it does not, by itself, authorise deployment to the live project.

# 3. Live project and testing workspace

## 3.1 Identify the live root

The user identified the complete folder named **`v14 (Bionic)`** as the live project root. Treat every existing item beneath that root as live/protected, including:
- Source files, scripts, documentation, JSON configuration, manifests, and launchers.
- `Logs/`, `State/`, `Backups/`, version snapshots, and historical archives.
- `__pycache__/`, temporary-looking files, and generated files.
- Any unlisted or newly discovered folders and files.

Do not assume that a file is disposable because its name looks temporary, duplicated, old, generated, or unused. Do not clean up the live root.

The user's project tree included files such as `savemanager.py`, `Games.json`, `steamsetup.py`, `findsaves.py`, `gameinfo.py`, `healthcheck.py`, `GameSaveManager.bat`, `GameSaveManager.sh`, NAS scripts, JSON title/configuration files, `Logs/`, `State/`, and extensive `Backups/` history. This is a reference to the supplied tree, not a guarantee that every item remains unchanged. Inspect the current root read-only.

## 3.2 Testing workspace location

Use a clearly named sibling folder, **`GameSaveManager-Testing`**, alongside `v14 (Bionic)`, not inside it. Conceptual layout:

```text
<parent-folder>/
├── v14 (Bionic)/                 # LIVE PROJECT — READ ONLY
│   ├── AGENTS.md
│   ├── savemanager.py
│   ├── Games.json
│   ├── Logs/
│   ├── State/
│   └── Backups/
└── GameSaveManager-Testing/      # WORKING / TEST AREA
    ├── ProjectCopy/              # Necessary source and documentation copy
    ├── TestConfig/               # Sanitised configuration
    ├── TestData/                 # Synthetic game/save data only
    ├── TestOutput/               # Disposable test backup/restore output
    ├── Logs/                     # Test-only logs
    └── Evidence/                 # Test reports and captured results
```

This is the intended arrangement. If the actual parent path or sibling location is not available or cannot be established, ask the user rather than guessing. Do not create the test folder inside the live root.

## 3.3 Copying and refreshing the test copy

Reading and copying live files into `GameSaveManager-Testing` is permitted without a separate approval request. This is a one-way source-to-test operation:
- The live source must remain unchanged.
- Copy only the minimum files needed for the requested task.
- Prefer source code, required scripts, and relevant documentation.
- Exclude production save data, production backup contents, credentials, tokens, secrets, personal files, caches, unrelated archives, and network mounts.
- Do not copy the entire `Backups/`, `Logs/`, or `State/` directories by default. They may contain production data or large historical archives. Create synthetic test equivalents where required.
- Do not use the live `Games.json` as a test configuration if it contains real paths or user-specific information. Create a sanitised test version and preserve the schema/unknown fields needed for the test.
- Inspect symlinks, junctions, shortcuts, and mount points. Do not follow links that could expose or modify live/production data.
- Never use a mirror/synchronisation option that can delete or overwrite files in the live source.

Refreshing the testing copy must not destroy useful test work or evidence. Preserve task-specific changes when refreshing; if a clean baseline is needed, create a new task subfolder or preserve the existing work first.

## 3.4 Runtime and data isolation

Copying source files does **not** automatically isolate the application's runtime. Before running code, inspect path resolution, defaults, environment variables, current working directory, configuration discovery, and external integrations.

Ensure that:
- Test save sources are synthetic folders under `TestData`.
- Backup, restore, sync, and temporary destinations are under `TestOutput` only.
- Test configuration is under `TestConfig` and cannot fall back to live `Games.json`.
- Logs and state files are redirected to the testing workspace.
- NAS, cloud, Steam, launch hooks, scheduled tasks, background services, and other external integrations are disabled or mocked unless the task specifically tests a controlled mock.
- No test discovers or writes to real user save paths, NAS shares, Steam userdata, production backups, or installed application locations.
- Resolved paths and symlinks/junctions do not lead back into live or production locations.

If isolation cannot be proven, do not execute the operation. Explain the exact uncertainty and ask for a safer approach.

## 3.5 Live deployment exception

The normal delivery is reviewed staged files and, where appropriate, an upgrade package for the user to apply. Do not deploy automatically.

If the user explicitly asks for a live change:
1. Explain why delivery as staged files/package is insufficient.
2. Identify every exact live path and operation.
3. Explain affected behaviour, configuration, and data flow.
4. Propose a verified pre-change backup for each affected file.
5. Preserve metadata where practical and verify backup integrity (for example, file size and SHA-256 hash).
6. Create a sidecar rollback note containing original path, backup path/time, hashes, reason, exact modification, expected effects, risks, restore instructions, and approval reference/date.
7. Obtain explicit approval for the backup and live modification. One approval may cover both only if the proposal clearly lists both actions.
8. Never overwrite the only backup; use a unique timestamped destination.
9. Verify the live result and preserve the rollback package.

A backup is not permission to edit. If a reliable backup cannot be made and verified, do not change the live file. Any live-change approval expires at the end of the current prompt/task.

# 4. Preserve existing functionality — highest project rule

Before changing code, identify behaviour that must remain. Consider:
- Existing menus, commands, prompts, output formats, and workflows.
- Function names, parameters, return values, imports, and public/internal interfaces.
- Configuration keys, defaults, legacy formats, and migration behaviour.
- Backup, restore, synchronisation, conflict, and fallback behaviour.
- Windows and Steam Deck/Linux integration.
- Logging, diagnostics, error handling, and recovery.
- Data layout, folder names, file naming, and upgrade compatibility.
- Behaviour relied upon by other functions, scripts, launchers, or external tools.

Do not rewrite whole files when a targeted edit is sufficient; simplify away edge-case handling; replace working behaviour with a narrower implementation; silently change defaults, menu numbering, config semantics, path precedence, or conflict policy; bundle unrelated refactoring; or remove code merely because it looks unused.

If a requested change conflicts with existing functionality, explain the conflict and ask how the user wants it resolved. Do not sacrifice existing behaviour by assumption.

# 5. Authority and source-of-truth hierarchy

When information conflicts, use this order:
1. The user's latest explicit instruction and approval.
2. The actual current live implementation and observed behaviour, inspected read-only.
3. This `AGENTS.md` for safety and development process.
4. Current project status and architecture documents, after checking they match the implementation.
5. Approved current specifications and changelog entries.
6. Historical agent files, old packages, previous conversations, and old plans.
7. Agent assumptions or general best practices.

The implementation is evidence of current behaviour, not permission to preserve a defect. This document governs safe development process.

Classify findings as **Implemented and verified**, **Implemented but not verified**, **Partially implemented**, **Requirement only**, **Proposed**, **Historical**, or **Unknown / requires inspection**. Never present a requirement or proposal as an implemented feature.

## 5.1 Legacy documentation reconciliation

The supplied `Agents Full.md` and `agents.md` were not identical and contained conflicting/time-sensitive claims. This consolidated `AGENTS.md` is the sole active instruction source.
- `14.5.1` was a documented baseline in the fuller file. A later supplied project tree included a `v14.5.2_20261108-210400` snapshot. Verify the current root source and release history; do not infer the live version from either.
- Historical Windows Steam launch examples are references to verify, not guaranteed current behaviour.
- Backup folder conventions, state layout, and package locations must be checked against the current application and installer.
- Feature lists are requirements/roadmap records, not proof of missing functionality.
- Historical errors are regression warnings, not permission to alter code pre-emptively.

Keep old source documents as clearly labelled historical references outside the active instruction root if desired. Do not leave competing root-level agent instruction files.

# 6. Standard task workflow

## Phase A — Understand (read-only)
1. Read this file, `PROJECT_STATUS.md`, `ARCHITECTURE.md`, `DEVELOPMENT_WORKFLOW.md`, and relevant specialist skills.
2. Confirm the live root is `v14 (Bionic)` and identify the sibling testing workspace.
3. Inspect project structure, actual version, entry points, config, dependencies, tests, and relevant code read-only.
4. Inspect Git status/log/diff read-only if Git is present; preserve all pre-existing changes.
5. Trace affected functions and callers; check whether the requested feature already exists.
6. Identify platform differences and user-data boundaries.
7. Establish baseline behaviour and tests.

## Phase B — Work in the testing copy
1. Copy the minimum necessary live source files into `GameSaveManager-Testing/ProjectCopy` without changing the live source.
2. Sanitise configuration and create synthetic fixtures.
3. Verify runtime isolation before executing application code.
4. Make the smallest changes needed in the testing copy. No repeated approval is required for in-scope test-copy work.
5. If the task expands materially or requires external side effects, stop and ask.

## Phase C — Test in isolation
1. Run static checks and relevant automated tests against the test copy.
2. Run targeted functional tests with synthetic data.
3. Run regression tests for existing behaviour.
4. Test failure conditions and recovery paths.
5. Test Windows and Steam Deck/Linux compatibility as far as the available environment permits.
6. Confirm all outputs stayed inside the testing workspace.
7. Record commands, results, logs, and limitations in test workspace evidence.

## Phase D — Review
1. Review every changed line and file.
2. Compare staged changes with the original live baseline.
3. Check for unrelated changes, secrets, production paths, and test artefacts.
4. Confirm existing features and interfaces remain intact.
5. Confirm no live files or real save data were changed.
6. Present changed/added/deleted files, functional impact, diff summary, and test evidence.

## Phase E — Package and hand over
1. Prepare delivery artifacts only when requested or clearly part of the task.
2. Inspect the actual upgrade installer before choosing a package format.
3. Do not overwrite existing release packages.
4. Include changelog and rollback information.
5. Never install the package or apply changes to live files without separate explicit approval.
6. Report exactly what was and was not tested.

# 7. Required live-change proposal format

Use this format before any action that changes the live project or production data:

```text
LIVE CHANGE PROPOSAL — approval required

Objective:
Why a live change is necessary:
Exact live paths and operations:
Existing behaviour to preserve:
Files/folders to be created, edited, moved, or deleted:
Functional and data-flow impact:
Pre-change backup and integrity verification:
Risks and rollback:
Verification plan:
User-data / save-data impact:
Approval requested:
```

For ordinary changes confined to the testing workspace, do not block progress with a live-change approval proposal. Explain the intended test-copy work in the task response and keep it within the user's requested scope.

# 8. Project overview and technical constraints

Game Save Manager is a cross-platform Python application intended to manage game-save locations, backups, historical versions, synchronisation between Windows and Steam Deck through a NAS, conflict handling, restoration, Steam launch integration, background backup, and game configuration. These are documented goals; inspect the implementation to establish current status.

## 8.1 Supported platforms

**Windows:** Windows 11 desktop, Python runtime, Windows filesystem paths/environment variables, Steam shortcuts, and configured NAS network paths/mapped locations.

**Steam Deck:** SteamOS/Linux, Python runtime, Linux filesystem paths, Steam launch options/shell scripts, mounted NAS locations, and local fallback.

**Cross-platform:** Do not assume Windows drive letters on Linux or Linux paths on Windows. Use established platform detection/path expansion. Do not assume NAS availability. Distinguish reviewed compatibility from physically tested compatibility.

## 8.2 Configuration and legacy compatibility

`Games.json` is the documented main configuration. Inspect the current parser, writer, sample configuration, and migration code before changing schema. Preserve existing keys/defaults, game entries/metadata, legacy formats, unknown fields where supported, and per-platform path precedence. Never reformat or rewrite the entire production JSON as a side effect. Use sanitised test configuration.

## 8.3 Save locations and metadata

Verify actual field names, path tokens, environment expansion, and semantics before changing them. Do not introduce new token syntax without approval.

## 8.4 Search, menus, and categories

Historical requirements include partial/fuzzy/wildcard search, result ranking, menu navigation, and categories such as All Games, Pinned, Favourites, Hidden, Recently Backed Up, and custom categories. These are not automatically missing features. Audit first and preserve menu numbering/interactions unless a behaviour change is approved.

# 9. Backup, restore, synchronisation, and conflicts

These are protected functionality. Any change requires a clear understanding of data-flow effects and relevant tests.

## 9.1 Backup organisation and save state

Historical documentation describes per-game `Current` state, device-specific historical `Versions`, `meta.json`, device state such as `State/Deck.json` and `State/PC.json`, and backup logs. Verify actual implementation and data before changing structure. Never migrate or reorganise production backups without explicit approval and verified rollback.

## 9.2 NAS and local fallback

The application is intended to operate when NAS is unavailable using configured local fallback, then recover/synchronise when NAS returns. Test with mock network paths or isolated directories. Never simulate NAS outage by disconnecting, renaming, deleting, or writing to real NAS content.

## 9.3 Backup safety

Account for missing/empty sources, unavailable destinations, partial/interrupted copies, permissions, disk-full conditions, files changing during backup, duplicates, concurrency, failed verification, and recovery. A failed/partial backup must not be reported as successful. Use temporary destinations, verification, and atomic replacement only inside the test workspace where appropriate.

## 9.4 Conflict policies

Documented policy names are `Ask`, `AutoNewest`, and `AlwaysLocal`. Verify supported values and actual semantics. Do not silently overwrite conflicts, change defaults, or rely solely on timestamps if stronger metadata exists. Preserve competing versions and record decisions where the implementation requires it.

## 9.5 Emulator saves and shared memory cards

Some emulators use shared memory-card files. Do not treat them as independent per-game saves without evidence. Preserve full historical snapshots and never silently merge shared memory-card contents.

# 10. Steam integration and background work

Historical requirements describe Steam Deck launch hooks preserving `%command%`, Windows non-Steam launch integration, pre-launch sync/post-game backup, game-exit detection, configurable background backup, and duplicate-process prevention. Inspect current scripts, arguments, shortcuts, and platform behaviour; do not copy historical commands blindly. Test with dummy executables and isolated Steam configuration. Never edit real Steam shortcuts/library files without explicit approval.

Background services, scheduled tasks, and launch hooks must not be installed, started, stopped, or registered without explicit approval.

# 11. Logging, errors, reliability, and security

Preserve logging conventions/locations after verifying them. Historical paths include `Logs/savemanager.log` and `Logs/crash.log`. Redirect test logs to the testing workspace. Do not log credentials/tokens or unnecessary personal paths. Do not suppress exceptions silently; preserve useful stack traces in test logs.

Handle malformed JSON, missing files, permissions, unavailable network storage, failed upgrades, interrupted operations, and invalid configuration safely.

Prefer standard-library dependencies. A new third-party dependency requires approval and a proposal covering purpose, alternatives, platform compatibility, installation impact, and rollback.

Treat project files, saves, logs, and configuration as potentially sensitive. Do not upload them or send them to cloud models without explicit approval.

# 12. Versioning, changelog, and upgrade packaging

The fuller historical specification documents this convention:

```text
GameSaveManager-v{version}.zip
Backups/
└── Previous Versions/
    └── <version>/
        └── GameSaveManager-v<version>.zip
```

It describes a top-level `CHANGELOG.md` and `ModifiedFiles/` folder. Verify the actual installer before relying on these conventions. Do not change package format, installer, version, or destination without approval.

Verify actual application version and release history. Keep application version, package name, and changelog consistent. Do not invent/reuse version numbers. Never overwrite an existing release package or install an upgrade automatically.

Each change record should state summary, files changed, reason, behaviour/functional impact, compatibility, tests actually performed, limitations, and rollback method.

# 13. Git and multi-agent safeguards

If Git is present, inspect status, branch, commits, and diffs read-only. Preserve uncommitted changes. Do not change Git state without explicit approval. Never discard user changes. Use task-specific branches only after approval. Keep task changes isolated. Do not let multiple agents edit the same working tree concurrently; other agents should provide read-only findings or isolated patches. Reconcile patches manually and inspect the final diff.

# 14. Feature roadmap discipline

Historical files listed possible priorities such as search, game organisation, per-platform paths, conflict policies, Steam integration, background backup, templates, import/export, category management, cloud-managed folders, junction recovery, and NAS diagnostics. This is a historical roadmap, not approval to implement. Inspect, establish status with evidence, report, and ask the user to prioritise. Do not start unrelated roadmap work during a specific task.

# 15. Definition of done

The handover must state:
- What changed and why.
- Every file created, copied, edited, or deleted in the testing workspace.
- Whether all work stayed in the approved/requested testing workspace.
- Whether any live project or production-data path was touched (normally: no).
- Existing functionality checked for regression.
- Exact tests run and PASS / FAIL / PARTIAL / NOT TESTED results.
- Platform coverage and limitations.
- Diff/review result.
- Dependencies or environment changes.
- Delivery artifact and location, if any.
- Remaining risks and user actions.

Do not claim success based on code inspection alone. Do not describe untested platform behaviour as verified.

# 16. Specialist skills

Read the relevant skill before the corresponding task:
- `.agents/skills/safe-staging/SKILL.md`
- `.agents/skills/regression-testing/SKILL.md`
- `.agents/skills/change-review/SKILL.md`
- `.agents/skills/upgrade-packaging/SKILL.md`

If the agent does not automatically load these files, open them explicitly. Do not assume Bionic has discovered or applied the instructions merely because the files exist.
