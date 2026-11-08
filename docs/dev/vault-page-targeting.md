# Vault page targeting: deployment and acceptance

## Root cause

Vault operations used the raw task ID instead of navigation's last owned session key, and selected the first origin/form-matching supervisor tab rather than the driver's current page. Exact `https://google.com` versus `https://accounts.google.com` mismatch is separately valid and remains enforced.

## Contract

Bind fill/save-login/enter-code to an operation-local original document witness, owned session key and session generation. Inspection runs through the navigation driver. Secret-bearing writes use only supervisor CDP, synchronously witness-guarded around the existing exact-origin guarded fill script; never CLI argv. Recheck driver and session after prompts/resolution AND supervisor selection. Nested save-login -> fill keeps its outer binding.

Browser Use and Camofox explicitly refuse even if a stale built-in session remains. Their current-target APIs need separate adapters; do not claim compatibility.

This is not global navigation serialization. Replacing the pinned document removes its witness and refuses at the write. A tab/session change detected before dispatch refuses. A change after final admission can leave a write to the originally approved document; strict revocation of an already-dispatched write is not claimed. No expression may redirect to an unmarked/new document.

## Executed evidence

- Existing classifier/backend unit suites: 82 passed. They intentionally unwrap the binding decorator and do not certify targeting.
- Real Chromium/CDP fixtures: 22 passed, bare and ::local session keys, wrong-tab, mismatch, foreign owner, navigation/tab/session changes during resolution and target selection, unsupported backend refusal.
- Real agent-browser 0.26.0 subprocess through production `_run_browser_command` plus public vault handler: 8 passed, including success, mismatch, tab switch and session swap. Only package discovery is isolated from fleet PM state; transport is not replaced.
- Combined hosted Linux workflow run 42455359295 at c18bf80ebe50f64507a2f713c93cddc20b15260b: **112 passed, 0 skipped**, artifact vault-target-results/vault-target.xml. Later CI wiring-only changes require fresh head CI proof before merge.
- Original 16 CDP fixtures against base source: 9 failed, 7 control passes (previous worker evidence).

Workflow is reusable and called by the allowlisted CI orchestrator, included in all-checks-pass needs. Workers App lane supports workflow pushes; interactive lane does not.

Local XML: ~/.hermes/cache/scratch/vault-pr115-smoke.xml. Hosted XML: ~/.hermes/cache/scratch/pr115-ci-artifact/vault-target.xml.

## Stage/load boundary

No runtime edits or gateway restart. User authorizes land and stage only, not restart. Supported runtime writer: fleet/deploy.sh with explicit --no-restart, subject to its current deploy-window gate. Never set DEPLOY_WINDOW manually to evade that gate. If only a restart-approved window can advance disk, report the staging blocker rather than manufacturing approval.

Disk-stage is not live: already-imported browser vault modules need a gateway-specific approved restart, then real-account tool proof. Fresh-process fixtures do not prove a long-lived gateway loaded code.

Rollback: revert via new PR and FF-only deployment rail; preserve deployment backup, never reset/edit the runtime tree. Google credential website metadata may be corrected via supported op item edit --url after exact item metadata lookup and Google-domain verification; never retrieve its password or weaken matching.
