# Development Workflow — Game Save Manager

This file operationalises `AGENTS.md`. If anything conflicts, `AGENTS.md` wins.

## 1. Session start — read-only inspection

1. Read `AGENTS.md`, `PROJECT_STATUS.md`, and `ARCHITECTURE.md`.
2. Read the relevant `.agents/skills/*/SKILL.md` files.
3. Treat the complete `v14 (Bionic)` folder as the live project and read-only.
4. Use `GameSaveManager-Testing` as a sibling folder, not a subfolder of the live project.
5. Inspect version, Git status, entry points, configuration, dependencies, and relevant code without changing the live project.
6. Identify pre-existing user changes and preserve them.
7. Restate the task and identify the minimum files needed.

## 2. Copy into the testing workspace

Reading and copying necessary live files into the testing workspace is permitted without a separate approval request. Copy in one direction only: live source → test copy.

Suggested layout:

```text
<parent-folder>/
├── v14 (Bionic)/                 # LIVE — READ ONLY
└── GameSaveManager-Testing/
    ├── ProjectCopy/
    ├── TestConfig/
    ├── TestData/
    ├── TestOutput/
    ├── Logs/
    └── Evidence/
```

Copy only the source, scripts, and documentation required for the task. Do not copy production save files, the full `Backups/` tree, production logs/state, credentials, caches, unrelated archives, or symlinks/junctions to live data. Sanitise `Games.json` where it contains real paths or sensitive information. Preserve only the configuration schema/fields required to test the feature.

Do not use copy/mirror commands with options that can modify or delete the live source. Refreshing the test copy must not discard useful staged work or evidence.

## 3. Implement in the testing copy

The user's task authorises relevant implementation work in the separate testing copy. No repeated approval is required for each test-copy edit. Keep changes within the requested task and:
- Make minimal targeted changes.
- Preserve existing interfaces, menus, defaults, configuration semantics, and compatibility.
- Record each changed file and reason.
- Keep baseline files untouched.
- Do not install dependencies, alter system settings, or change Git state without approval.
- Stop and ask if scope materially expands or external side effects become necessary.

## 4. Prove runtime isolation before execution

Before running the application or tests, inspect configuration resolution, working directory, environment variables, path expansion, defaults, symlinks/junctions, and integrations.

Confirm:
- Test save sources are synthetic and inside `TestData`.
- Backup/restore/sync destinations are inside `TestOutput`.
- Config, state, and logs resolve inside the test workspace.
- The app cannot silently fall back to live `Games.json` or real user paths.
- NAS, Steam userdata, launch hooks, scheduled tasks, and background services are disabled or mocked.
- No resolved test path reaches the live project or production data.

If any write location cannot be established or isolated, do not execute the operation. Explain the uncertainty and ask for direction.

## 5. Verify

Run appropriate checks against the testing copy:
- Syntax/compile and import checks.
- JSON/schema and configuration round-trip checks.
- Existing automated tests.
- Focused functional tests.
- Regression tests for existing behaviour.
- Failure-path, interruption, and recovery tests.
- Windows and Steam Deck/Linux checks where available.

When a test finds an error, investigate and correct it in the testing copy, then rerun the relevant tests. Continue until relevant tests pass or a genuine blocker is identified. Do not suppress failures or stop after the first failure without explaining why further testing is impossible.

Classify each test as `PASS`, `FAIL`, `PARTIAL`, or `NOT TESTED`. A syntax check is not a functional test. Testing one platform does not prove compatibility on the other. Capture exact commands, fixtures, expected/actual results, and evidence paths under `Evidence/` or `Logs/`.

## 6. Review and handover

- Review the full diff against the untouched live baseline.
- List every added, modified, and deleted test-copy file.
- Confirm no unrelated changes, secrets, production paths, or test artefacts are included in delivery.
- State which existing features/interfaces were checked.
- State whether any live or production-data path was touched (normally no).
- Report exact test outcomes and platform limitations.
- Identify residual risks and untested areas.

## 7. Package and deployment

Creating a delivery package is distinct from applying it. Verify the actual installer format before packaging. Do not overwrite existing packages. Do not install, deploy, or copy staged files into `v14 (Bionic)` without a separate explicit live-change proposal and approval for the current prompt.
