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

## Explicit outstanding acceptance

- Full notifier/owner-wake dispatch/admission through real executor against isolated scratch gateway, with ALR/same-vendor exhaustion and permitted CPA fallback. Current auto-wake test calls run_entry, not the whole dispatcher, and uses synthetic openrouter/openai protocols.
- Real detached subprocess across two profile homes with independent endpoints/credentials; current detached evidence uses shared production reconstruction in-process.
- Full final-head regression receipt. Linux wake suite stalls in hermes_state_dbfile /proc fd traversal under home_io_guard; detached safety has two process-launch failures in test-gate's systemd environment. Neither is called green or attributed to base without a base run.
- Mutation attribution is currently broad AssertionError capture and must be narrowed to named sentinel/route assertions.
- Final mixed/explicit replay regression, malformed durable policy, corrupt persisted identity, all real route-writer concurrency and pin-refused downgrade coverage.
- No production provider test, merge, deploy, gateway restart, or production smoke has occurred. Apollo owns landing and separately approved activation.

No production configuration, runtime tree, board fixtures or gateway processes were modified.
