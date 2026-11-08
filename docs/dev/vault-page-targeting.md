# Vault page targeting: deployment and acceptance

## Root cause (base 59e169b2c3f4a4d743395cdb10dc152fc3d5b04c)

- `tools/browser_vault_tool.py:67,99`: supervisor lookup used the raw task ID, whereas navigation/non-nav tools use `_last_session_key` (`tools/browser_tool.py:347-363`). A hybrid `::local` session could inspect one driver page and write through another supervisor.
- `tools/browser_vault_tool.py:472-479` searched every saved origin with `_focus_bound_origin` (`202-213`). `CDPSupervisor.focus_page` selects the first origin/form match (`tools/browser_supervisor.py:266-313`), not the navigation driver's page. Without a match, vault origin reads use the supervisor's initial page, explaining a blank/new-tab origin despite navigation elsewhere.
- Exact `https://google.com` versus `https://accounts.google.com` is a separate valid refusal. This patch does not change or broaden saved-origin authorization.

## Change

Bind fill, save-login and enter-code to the driver's current document using an operation-local random nonsecret witness. All inspection uses the same owned session key. Supervisor target selection requires that exact witness, not credential origin/form shape. Recheck driver document and session generation after resolution/prompt; the secret-bearing CDP expression checks the witness synchronously around the existing atomic origin check. Secrets never go through CLI argv.

The nested save-login -> fill retains its outer document witness. Missing/foreign sessions refuse before page IO. Browser Use's independently-managed tabs and Camofox have no supported current-page witness adapter here and fail closed; adding one needs their authoritative current-target API, not an origin scan. Do not claim those backends work from this patch.

## Executed acceptance

`test-gate --local <worktree>/.venv/bin/python -m pytest tests/tools/test_browser_vault.py tests/tools/test_browser_vault_cards.py tests/tools/test_browser_vault_target.py -q --tb=short`

- Fixed tree: **98 passed**, including **16 real Chromium/CDP cases** (bare and `::local` session keys).
- New 16-case file against original production source: **9 failed, 7 passed**. Includes real writes to the wrong document during resolution races; foreign task trips the driver boundary. The 7 survivors are controls/old-path early refusals, not claimed individually mutation-sensitive.
- Same-origin and cross-origin navigation, driver tab switch, session generation replacement, navigation after supervisor target selection, unrelated task, and exact-origin mismatch all fail closed on fixed code; untouched sibling fields read back empty.
- Existing classifier/backend unit suites explicitly unwrap the new page-binding decorator, retaining their existing page-IO mocks. They do not certify page binding. The new Chromium file certifies that boundary, using synthetic nonsecret address data only.
- Full-tree suites were not run locally (fleet policy). A dedicated Playwright CI workflow was prepared but the GitHub App refused the push because it lacks workflows permission. That workflow was removed from the commit so the code can be reviewed. Ordinary Python collection skips the Chromium file without Playwright; release is blocked until an authorized CI lane installs Playwright/Chromium and runs these 16 cases (or a supported off-box receipt covers the exact head).

## Live-load boundary

No runtime edit, deployment, gateway restart, real credential read or Gmail mutation was performed. Runtime HEAD observed: `b7f04bc8a53f5beba237024915ec968c0b703f04`.

After review and green required CI, land through `~/.hermes/scripts/fleet-merge.sh` with exact reviewed head and actor attribution. Deploy only through `bash ~/.hermes/fleet/deploy.sh --no-restart` if explicitly approved to advance disk without restarting. Do NOT run bare deploy.sh (default restarts fleet). A running gateway has already imported the old module; disk staging alone does not repair that process. Obtain explicit restart approval and use the supported safe-restart rail. Verify runtime-tree/finder guards and run this fixture on deployed bytes in a fresh process, then retry the original browser tool flow without sending any secrets to logs.

Rollback: revert the landed fix through a new PR and the same FF-only deploy/restart rail; do not reset the live tree or copy worktree files into it. Stop credential-fill attempts if post-deploy identity checks fail. A metadata-only correction for an actual saved Google website must be separately user-approved; never weaken exact-origin matching.

## Remaining review/verification

The CLI current-page boundary in the Chromium fixture is a page-driver adapter, not a real agent-browser executable invocation. A post-restart tool-session smoke remains required. Concurrent same-task navigation after the last driver read is confined to the pinned document by the synchronous witness; it is not globally serialized with all browser tools. Review this contract before release.
