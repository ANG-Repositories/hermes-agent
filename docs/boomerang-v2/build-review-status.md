# Parent routing build checkpoint

Task: t_99abe991. Implementation is partial; not approved for landing or activation.

## Executed evidence

- Base route matrix: 3 failed / 5 passed; inherited summary/fork/branch incorrectly selected worker-model. Explicit child and ordinary controls passed.
- Cold-parent base probe attempted global credential resolution instead of its supplied session resolver and failed with no configured global provider.
- Routing/fork matrix checkpoint: 55 passed in 43.88s.
- Existing persisted-session/channel suites: 15 passed in 10.66s.
- Existing ordinary fallback matrix: 29 passed in 9.13s.
- Latest HTTP checkpoint: 10 passed in 31.59s. Synthetic primary and same-provider rung returned quota 429; cross-vendor SSE returned the unique expected nonce. Includes actual run_entry wrapper with executor/delivery mocked, canonical SessionStore cold resolution, active fallback, empty-policy primary-only failure, in-process detached JSON reconstruction and two boundary mutants.
- Actual credential-sync writer plus route matrix/eligible-fork subset: 20 passed in 11.23s.
- Final targeted mixed/explicit per-task replay, actual credential-sync and bounded snapshot wait: 5 passed, 18 deselected in 3.86s. Import-at-collection corrected a local bootstrap/home-I/O guard fixture error before this receipt.

Runnable narrow checks (sanctioned test-gate; local fallback only when remote admission is unavailable):

    ~/.hermes/scripts/test-gate python3 -m pytest -q tests/tools/test_boomerang_parent_routing.py tests/gateway/test_boomerang_parent_routing.py
    ~/.hermes/scripts/test-gate python3 -m pytest -q -s tests/integration/test_boomerang_parent_routing_http.py

HTTP cases enforce 30-second alarms; bound the execution process to 180 seconds externally, excluding test-gate placement queue time.

## Independent review disposition

1. Explicit recovery credentials bypass: accepted, bypass now checks persisted route_source; selected per-task route also recorded.
2. Mixed batch child-0 route contamination: accepted, selected route and effective fallback recorded on each child/task, including ordinary and explicit children. Both-order and explicit replay checks passed in the final targeted receipt.
3. Credential refresh snapshot race: accepted, refresh/adoption writers synchronized per agent; actual sync-writer regression passes. Snapshot acquisition bounded to two seconds. Token lookup/client operations still occur under the per-agent lock; no gateway-wide lock is used.
4. Eligible-fork nested override aliasing: accepted, deep copy from captured route snapshot; eligible fork/summary test added.
5. Postconstruction downgrade re-snapshot: accepted, captured route snapshot threaded through recursive rebuild. Dedicated parent-switch downgrade/pin-refused proof remains outstanding.

## Continuation checkpoint (same draft PR #116)

- Endpoint-loss reproduced directly on cc36bb1: `persist_route` omitted selected URL. Selected safe primary endpoint now persists in both actual child metadata and helper records; recovery prefers it over provider default and preserves selected protocol. Credential-bearing URLs fail closed. Eleven persistence cases passed through remote sanctioned gate (ci), 3.17s; direct pre-fix assertion was `selected endpoint lost before recovery`.
- Hermetic HTTP fixture now suppresses unrelated plugin/relay boot and context probes, keeps SQLite registry checks for private new DBs, and shortens the actual retry sleep at both definition and imported call-site seams. Cold first summary alone passed 1 in 8.97s. No deadline increased.
- Real notifier dispatch -> persisted claim -> busy/cold canonical parent -> actual delegate/conversation/fallback -> nonce verdict -> real executor/receipt -> adapter delivery: 3 passed in 62.95s before DB-fixture cleanup. Combined HTTP + auto-dispatch rerun: 13 passed in 73.19s. ALR-shaped primary and same-vendor `/anthropic/v1/messages` each fail; CPA-shaped OpenAI chat/SSE succeeds on `/v1/chat/completions`, with distinct synthetic credentials. This is protocol integration, not vendor availability.
- Actual two subprocess/profile homes use production `_child_main`, profile `.env` credential resolution, persisted task reconstruction and real local HTTP: 1 passed in 28.20s. Each returned its own nonce on its own selected URL/model/key despite changed worker/provider defaults. Plugins/MCP startup and metadata probes are explicitly suppressed by child harness; actual platform launch/supervisor admission is not claimed.
- Snapshot-timeout arm holds the real per-agent lock: zero provider requests, terminal failed claim retains original wake, `resurfaced=true`, `resurface_delivered=false` after adapter refuses admission. Owner obligation is not lost.
- Newly discovered: documented pin-refused summary reconstruction did not exist in this tree (no `pin_refused` code). Implementing narrow explicit relay-503/header refusal propagation and one summary rebuild using captured route; fingerprint/pin-refused constructor tests pending.
- Existing Linux wake hang reproduced: default SessionDB open traverses host `/proc/*/fd` under home-I/O guard. Passing `HERMES_STATE_DELETED_WAL_CHECK=registry` in outer shell is scrubbed by test isolation; set it in the scratch-DB fixture. Prior multi-file run stopped bounded after no result, not called green. Base comparison remains pending.

Raw receipts are `/Users/alexgierczyk/.hermes/cache/scratch/boomerang-{persistence-green,http-cold-first,http-final,auto-wire-r2,detached-profiles}.log`.

## Final continuation receipt

All commands use `TEST_GATE_NO_CACHE=1 ~/.hermes/scripts/test-gate /opt/homebrew/bin/python3 -m pytest -o addopts= -q` followed by the named repo-relative files (HTTP also `-s`). Remote venue was ci unless noted. Each tranche is at most three files; no suite guard bypass, production calls, board writes, config changes, merge, deploy or restart.

| Targets | Result | Raw scratch receipt (`boomerang-` prefix) |
|---|---|---|
| `tests/integration/test_boomerang_auto_routing.py`, `test_boomerang_detached_profiles.py`, `test_boomerang_parent_routing_http.py` | 15 passed, 78.22s | `integration-final-r2.log` |
| `tests/tools/test_boomerang_fingerprint_routing.py`, `test_boomerang_route_persistence.py`, `test_boomerang_parent_routing.py` | 45 passed, 26.21s | `final-contract-r2.log` |
| `tests/gateway/test_boomerang_parent_routing.py` with earlier fingerprint/persistence subset | 22 passed, 34.46s | `final-contract.log` |
| `tests/tools/test_delegate_tool_fork_mode.py`, `test_delegate_tool_fork_entrypoint.py`, `test_delegate_tool_fork_pin.py` | 49 passed, 77.41s | `fork-regression.log` |
| `tests/tools/test_delegate_fallback_matrix.py`, `test_delegate_request_overrides.py`, `test_boomerang_inherit_context.py` | 61 passed, 17.54s | `ordinary-regression-r2.log` |
| `tests/tools/test_delegate.py`, `test_delegate_detached.py` | 187 passed + 3 subtests, 138.16s | `delegate-regression-r3.log` |
| `tests/tools/test_delegate_detached_safety.py`, `test_delegate_restart_recovery.py` | 98 passed, 1 skipped, 194.89s | `detached-regression-r2.log` |
| `tests/gateway/test_kanban_boomerang_wake_dispatch.py` | 62 passed, 252.14s | `wake-regression-r2.log` |
| `tests/gateway/test_session_model_override_persistence.py`, `test_session_model_override_routing.py`, `test_session_route_concurrency.py` | 16 passed, 64.00s | `session-regression.log` |

The persistence red control materialized cc36bb1's routing helper in a separate base archive: 12 failed in 2.44s, including named endpoint-loss assertion, malformed policy before lookup and credential URL refusal. This is not the queued file initially named `persistence-red.log`, which actually ran after the fix; only the direct pre-fix assertion and `persistence-control-red.log` are red evidence.

Regression failures were adjudicated, not waived:
- Existing detached launch tests failed with the same `None` handle / missing env assertions on immutable base 59e169b (2 failed, 1 passed in 17.88s, `base-comparison.log`). Host systemd scope admission returns degraded. Their tests now explicitly inject direct-launch admission while retaining actual Popen, PID-file failure and env assertions. Whole files pass as above.
- Wake setup stalls were captured with a 15s faulthandler: `/proc` scan, then SQLite `_init_schema.executescript` under remote load. Base exact owner-wake node passes remotely and locally. Scratch fixture now retains registry WAL checks without foreign-host fd enumeration. Whole head file passes, no deadline increase.
- Expanded route metadata exposed MagicMock-generated non-configuration objects (deepcopy reached a thread lock). Existing mock parent factories now declare concrete route configuration, including nested orchestration. No production copying guard was weakened; complete ordinary/delegate files pass.
- Both boundary mutants now accept only named routing-contract assertion failures. Fingerprint mismatch and explicit relay `503 + x-pool-pin-refused` rebuild tests switch the live parent mid-construction/request, then require the original model/provider/URL/key/chain and summary context. Refusal controls reject 429, missing header, summary mode and an unpinned request. Pin-refusal transport is injected at the real request method; this is not a relay vendor test.

Sanitized per-case traces are committed at `evidence/continuation-wire-traces.json`. Actual synthetic auth equality is asserted by the tests; credential values are omitted from that published artifact.

## Remaining limitations — retain draft pending parent certification

- Production activation/smoke is intentionally pending; no restart or real provider availability is claimed.
- Independent final-head review/certification belongs to Apollo. Full-tree CI was not run locally; these are named narrow subsystem receipts.
- Detached subprocess proof uses two actual OS processes and the real `_child_main`/record reconstruction/credential resolver, but suppresses plugin/MCP startup and does not certify OS supervisor spawn admission or restart/relaunch exactly-once integration. Two profiles use distinct endpoints/keys/models with explicit disabled chains; cross-vendor traversal is proven separately through automatic wakes.
- Cold session/channel precedence and corruption refusal are separately tested; not every precedence/corrupt-on-disk case has its own full HTTP dispatch row. Actual credential-sync writer concurrency is proven, but decorator-removal mutation coverage for every route writer is not exhaustive.
- Resolved by operator commit f4738af4fda2: cold construction logs `session-rehydrated` provenance while retaining the internal inheritance policy used by recovery. Red-first provenance regression failed before the fix; 26 routing/session tests passed afterward. Independent operator integration rerun at f4738af: 15 passed in 31.67s, exit 0. Final scoped independent delta review APPROVE; production activation remains pending.
- HTTP alarms remain 30s per case; combined integration receipt is below 180s, but test-gate placement time is outside that bound and no independent 180s process-group supervisor is shipped.

No production configuration, runtime tree, tracking board or gateway process was modified.
