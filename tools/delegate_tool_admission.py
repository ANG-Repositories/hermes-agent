"""Release constructed, unstarted children after background admission refusal."""

import json
import logging

logger = logging.getLogger(__name__)


def reject_background_admission(*, reason, children, task_list, live_deleg_id, live_writers,
                                dispatch=None):
    from hermes_cli.observability.shared_metrics_loop import finish_delegation_unit
    from tools.delegate_tool import _teardown
    from tools.delegation_live_log import update_manifest_statuses

    results = []
    for index, child in enumerate(children):
        _teardown(child, "admission_rejected")
        entry = {"task_index": index, "status": "rejected", "reason": reason,
                 "summary": "Background admission rejected; child did not start.",
                 "api_calls": 0, "duration_seconds": 0.0}
        results.append(entry)
        if index < len(live_writers) and live_writers[index] is not None:
            try:
                live_writers[index].finalize(entry)
            except Exception:
                logger.debug("Rejected transcript finalize failed", exc_info=True)
    update_manifest_statuses(live_deleg_id, results)
    finish_delegation_unit(task_list, results, background=True)
    if dispatch and dispatch.get("fallback_event_id"):
        from tools.async_delegation import acknowledge_outbox_event

        acknowledge_outbox_event(
            dispatch["fallback_event_id"], outcome="dropped", reason="admission_rejected",
            profile_home=dispatch.get("_registry_profile_home") or None,
        )
    return json.dumps({
        "status": "rejected", "mode": "background", "started": False,
        "reason": reason, "retryable": True,
        "note": "No subagents started or queued. Continue the parent conversation; retry "
                "delegation after capacity or persistence recovers. Do not wait or poll.",
    }, ensure_ascii=False)
