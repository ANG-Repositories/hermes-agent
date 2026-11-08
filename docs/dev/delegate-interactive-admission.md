# Interactive delegation admission

## Contract and scope

A background caller with an existing later-result consumer must never become a
foreground child join because admission failed. The existing session-capability
and wake-session resolution remains authoritative. Finite callers still join;
recovered, already-admitted units still use their synchronous recovery runner.
No new queue, config, cap increase, registry purge, or live restart is included.

## Grounded branch census

`delegate_task` currently joins after durable-spec construction fails, after
`registry_cap`/`registry_error`, and after any other dispatch refusal. These are
new-dispatch failures, unlike the finite-session join before dispatch and the
recovered runner's final synchronous path. `async_delegation._dispatch_admitted`
checks capacity and durable persistence before submitting the worker. Submission
failure removes its own in-memory/SQLite reservation and terminalizes its own
durable attempt; its fallback event must be acknowledged as an inline rejection,
not reported later as a second completion. Detached/kanban launch fallback runs
inside an already-admitted worker and is unchanged.

The incident registry was reported by Apollo as exactly 256 retained terminal
records, not 256 active children. `_prune` stops at the cap while admission rejects
at the cap; retention has not expired. Changing that retention policy is outside
this fix: an exact-full store must reject promptly, whether settled or pending.

## Implementation decision

One sibling helper closes only the constructed children through the existing
teardown door, finalizes their transcripts as rejected, ends the metrics unit,
and returns `status=rejected`, `mode=background`, `started=false`, a stable
`reason`, `retryable=true`, and a safe next step. It never returns an accepted
handle or executes a child. All three foreground fallback sites use it. Existing
submission-failure outbox acknowledgement is retained with truthful rejection
wording. Pool capacity and backend retirement receive explicit reason codes.

## Verification and rollout

Run `scripts/run_tests.sh tests/tools/test_delegate_admission_rejection.py -q`
on an isolated ci-box checkout. Each failure arm drives real delegate dispatch
with a blocked sentinel child; the parent must return before sentinel release,
with zero executions. The pre-fix run failed all five arms at the bounded parent
return assertion (5 failed in 20.76s). Fixture cleanup releases and joins the
sentinel even on failure. Add success/completion and finite compatibility controls,
then restore old branches as mutation controls. Integration must prove a second
parent request completes before releasing a successful child and one completion
after release, within 60 seconds. Live original-conversation acceptance and
restart authorization belong to Apollo, not this implementation run.

Rollback is a reviewed code revert through normal deployment; preserve all
registry/session data. Existing foreground waits are not retroactively repaired.
