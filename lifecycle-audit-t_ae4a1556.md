Kanban lifecycle audit — t_ae4a1556

Base: c1524a12afa6c9ae6997931c6f44c9a52ba9469e
Live board read-only observations on 2026-11-08:
A review without review_requested: 16.
B blocked with null block_kind: 30.
C ready with human/human:* assignee: 216 (changed since original 215-card census).
D scheduled with null next_eligible_at: 1.
No live repair or schema change has been performed by this run.

Acceptance conflicts found:
E's two cards both have after-merge edges, not ordinary blocks edges. Existing
kanban_after_merge.py explicitly preserves those gates even for done/archived
parents with recorded PRs. REST: ANG-Ventures/hermes-home#5971 closed/unmerged;
#8993 closed/merged. t_a75816c1 therefore cannot safely be promoted on parent
closure; its prerequisite was never merged. t_88ae92c4 can use the normal merged
edge reconciler. Do not weaken _parents_satisfied to ignore after-merge.
D t_d394f63b is intentionally waiting on Ace saying parts arrived. Its scheduled
event explicitly says PARKED, waiting on parts arrival. Repair to a human input
hold, not an invented timestamp.
B unblock_when is not a column in this runtime. The predicate reader and grammar
live in hermes-home scripts/kanban-unblock-when-sweep.py and hooks/unblock_when.py.
This repo has durable obligations but cannot determine that a placement rule has
cleared without coupling to the separate placement/predicate implementation.
The task explicitly forbids widening into placement/admission.
A #124 is OPEN, not landed on the base. Its named kanban_review_handoff.py file
does not exist on current fork/main. Avoid assuming that writer was already fixed.

Partial implementation, NOT review-ready:
D wake_at on schedule_task; CLI passes it in the same transaction as scheduling;
dashboard payload exposes it. Persistent INSERT/UPDATE trigger rejects scheduled
without a wake. Narrow new invariant tests red 2/2 before, green 2/2 after.
A persistent review-entry repair trigger synthesizes missing implementer
provenance from last successful run / prior assignee / human:operator. Needs
more coverage (especially initial INSERT and coexistence with normal handoff).
C human ready entries route to todo with one needs_operator event; recompute_ready
skips human assignments until reassigned. Needs operator visibility/notification
validation and complete migration.
A/C tests red 2/2 before, green 2/2 after.

Verification actually executed on ACE-AI:
cd /srv/ci/scratch/t_ae4a1556 && scripts/run_tests.sh tests/hermes_cli/test_kanban_schedule_atomic.py -q
Before: 2 failed in 0.63s. After: 2 passed (runner wall 2.3s).
cd /srv/ci/scratch/t_ae4a1556 && scripts/run_tests.sh tests/hermes_cli/test_kanban_lifecycle_entries.py -q
Before: 2 failed in 1.18s.
Combined current run:
scripts/run_tests.sh tests/hermes_cli/test_kanban_lifecycle_entries.py tests/hermes_cli/test_kanban_schedule_atomic.py tests/hermes_cli/test_kanban_schedule_wake.py -q
6 passed, 11 failed. The 11 failures are existing schedule-wake tests calling
schedule_task without wake_at. These require intentional fixture/contract updates,
not a relaxation of the new scheduling invariant. Other existing schedule callers
were inventoried using search_files but not migrated yet.
Local scripts/run_tests.sh was refused by local-suite-guard; did not bypass it.
Remote runner used its own scrubbed temp HERMES_HOME. No live deviations.

Remaining work:
B kind/predicate policy and red-first implementation; E resolution decision;
A full writer audit and repair; C one-shot per-card map and operator visibility;
D old fixture/caller migration, documentation, safe event-hold repair;
one-shot dry-run/apply with reviewed manifests and before/after live counts;
per-class commits, PR and exact-head CI.

Durability:
Task-owned branch wt/t_ae4a1556 pushed to ANG-Ventures/hermes-agent (fork).
No PR opened: incomplete implementation with known test failures.
Remote test slice retained at /srv/ci/scratch/t_ae4a1556 for continuation.
