#!/usr/bin/env python3
"""t_a00081df: per-run failed-job + venue census on hermes-agent-ci main CI runs."""
import json, subprocess, sys, collections

R = "ANG-Ventures/hermes-agent-ci"


def api(path):
    import time
    for attempt in range(5):
        p = subprocess.run(["gh", "api", "--paginate", path], capture_output=True, text=True)
        if p.returncode == 0:
            break
        time.sleep(3 * (attempt + 1))
    else:
        print("SKIP", path, p.stderr.strip()[:100], file=sys.stderr)
        return []
    out = p.stdout
    # paginate concatenates JSON objects
    dec = json.JSONDecoder(); i = 0; objs = []
    while i < len(out):
        while i < len(out) and out[i].isspace():
            i += 1
        if i >= len(out):
            break
        o, i = dec.raw_decode(out, i); objs.append(o)
    return objs


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    runs = api(f"repos/{R}/actions/workflows/388948865/runs?branch=main&status=completed&per_page=100")[0]["workflow_runs"]
    runs = [r for r in runs if r["conclusion"] != "cancelled"][:n]
    rows = []
    for r in runs:
        jobs = []
        for o in api(f"repos/{R}/actions/runs/{r['id']}/jobs?per_page=100&filter=latest"):
            jobs += o["jobs"]
        for j in jobs:
            host = (j.get("runner_name") or "").split("-ang")[0]
            dur = None
            if j.get("started_at") and j.get("completed_at"):
                from datetime import datetime
                f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
                dur = (f(j["completed_at"]) - f(j["started_at"])).total_seconds()
            rows.append({"run": r["id"], "job": j["name"], "jid": j["id"], "host": host,
                         "concl": j["conclusion"], "dur": dur})
    json.dump(rows, open("/tmp/t_a000_jobs.json", "w"))
    c = collections.Counter((x["job"], x["host"], x["concl"]) for x in rows if x["concl"] == "failure")
    for (job, host, concl), k in sorted(c.items(), key=lambda t: -t[1])[:60]:
        print(k, host, job)


if __name__ == "__main__":
    main()
