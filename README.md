# Game Save Manager — Agent Instruction Pack

This pack provides a single active instruction source and supporting workflow/status/architecture documents for AI coding agents, including LM Studio Bionic.

## Contents

- `AGENTS.md` — authoritative project and safety instructions.
- `PROJECT_STATUS.md` — verified project-state tracker; starts with key implementation details unverified.
- `ARCHITECTURE.md` — documented system responsibilities and architecture discovery checklist.
- `DEVELOPMENT_WORKFLOW.md` — practical session, staging, testing, review, and handover process.
- `.agents/skills/safe-staging/SKILL.md` — live/test separation and runtime isolation.
- `.agents/skills/regression-testing/SKILL.md` — regression matrix and evidence requirements.
- `.agents/skills/change-review/SKILL.md` — staged diff and regression review.
- `.agents/skills/upgrade-packaging/SKILL.md` — safe package preparation without deployment.

## Project folder arrangement

The user has identified the complete `v14 (Bionic)` folder as the live project. Keep it read-only. The working test folder should be a sibling named `GameSaveManager-Testing`:

```text
<parent-folder>/
├── v14 (Bionic)/                 # LIVE PROJECT — READ ONLY
└── GameSaveManager-Testing/      # TESTING / WORKING COPY
    ├── ProjectCopy/
    ├── TestConfig/
    ├── TestData/
    ├── TestOutput/
    ├── Logs/
    └── Evidence/
```

Do not put the testing folder inside `v14 (Bionic)`. This avoids test files being swept into live backups, release archives, or project operations.

## How Bionic should use this pack

At the start of each project/session, explicitly read `AGENTS.md`, `PROJECT_STATUS.md`, `ARCHITECTURE.md`, `DEVELOPMENT_WORKFLOW.md`, and the relevant specialist skills. Do not assume automatic discovery.

The user permits read-only inspection and one-way copying of necessary source files from the live project into the testing folder without a separate approval request. The user's task also permits in-scope edits and isolated tests inside the testing copy. Approval is required before any change to the live project or production data; approval applies only to the exact actions/paths agreed for that prompt.

Before executing application code, Bionic must verify that configuration, saves, backups, logs, state, and external integrations are isolated. A copied project can still contain hard-coded paths to real saves, NAS shares, or Steam userdata. If isolation cannot be demonstrated, do not execute the operation.

## Suggested Bionic session-start instruction

> Read `AGENTS.md`, `PROJECT_STATUS.md`, `ARCHITECTURE.md`, and `DEVELOPMENT_WORKFLOW.md`, plus the relevant specialist skills. Treat the complete `v14 (Bionic)` folder as the live project and read-only. Use the sibling `GameSaveManager-Testing` folder for all implementation and testing. You may inspect and copy the minimum required source files into that test folder without asking for separate approval. You may make in-scope edits and run tests inside the isolated test copy as part of my requested task. Do not change live files, real saves, production configuration, NAS content, Steam userdata, installed application files, or live backups. Any live change or deployment requires a precise proposal and my explicit approval for the exact paths/actions for this prompt only. Verify runtime isolation before executing code, preserve every existing feature, test relevant regressions, and report actual PASS/FAIL/PARTIAL/NOT TESTED results. Never claim a test passed unless it was run.

## Technical limitation

These documents communicate mandatory agent behaviour; they are not an operating-system sandbox. For stronger enforcement, keep the live project outside the agent's writable workspace or apply OS-level read-only permissions/snapshots and use a separate development account/workspace. Confirm Bionic's actual workspace and tool permissions.
