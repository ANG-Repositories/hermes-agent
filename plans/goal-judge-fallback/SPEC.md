# Bounded goal-judge fallback

Scope: judge verdicts only, not contract drafting, main models, delegation or live configuration.

Evidence: root agent.log records goal-judge API failures on 2026-11-07 at 11:17:37, 23:15:32 and 23:22:39: HTTP 503, `pool at capacity`. Logs do not identify the original paused session; no attribution of those three errors to it is claimed. Existing auxiliary fallback performs same-provider retries and recovery, and accepts the first successful HTTP response before goal schema validation. Existing goal parsing coerces arbitrary truthy `done` values into completion. Those mechanisms cannot establish the requested bounded verdict-chain contract.

Design:
- Opt in through `auxiliary.goal_judge.fallback_chain`; empty/absent preserves the current routing path. Configured primary plus at most seven explicit fallback routes; validate provider/model, finite positive per-attempt timeout and total_timeout before dispatch. No implicit main-provider/discovery fallback in this mode.
- Visit each declared route once in order. Each subprocess uses the existing auxiliary call/router with recovery disabled for this single attempt, fresh client state and route-local configuration. No duplicate resolver. Parent enforces a monotonic per-attempt deadline (including process startup/resolution), clamped to total_timeout, and reaps/kills the owned process on timeout, interrupt or stop. No thread abandonment. Credentials come from the owning profile's child environment, never from durable artifacts.
- One strict valid verdict ends the chain, including blocked/wait/continue. Legacy `done` accepts booleans only. Empty, malformed or non-schema responses are failures, never done. Availability/configuration failures may advance to the next rung; other errors terminate visibly. Errors and telemetry expose category and sanitized route labels only, not prompts, response excerpts or exception messages.
- Preserve existing GoalManager failure accounting: one result per complete chain; successful valid verdict clears counters, exhausted chain increments once. Cancellation propagates as a BaseException and never becomes a verdict. Goal state remains subject to its existing CAS fence.
- Parent applies fleet config/deployment separately. Registry aliases observed read-only: opus = claude-alr/claude-opus-5-5; sol = cpa/gpt-6.1-sol; grok = xai-oauth/grok-4.7; kimi = cpa/kimi-k3.1-pro. These are proposed sample bindings, not served-identity evidence.

Acceptance: narrow runner tests against real judge/GoalManager and real HTTP transports in isolated homes; sequential success, primary short-circuit, exhaustion, malformed/schema failures, blocked termination, deadline/cancellation cleanup, counter reset and A-B-A profile separation. Live fallback canary requires served response identity and must not mutate fleet config. Do not equate source tests with deployment.

Rollback: remove fallback_chain/total_timeout to use legacy routing; revert this PR to remove implementation. No live files are changed by this task.
