"""List FAILED/ERROR test ids + target-file durations in failing jobs of hosted main CI runs."""
import json, re, subprocess, sys
from collections import Counter, defaultdict

REPO = "ANG-Repositories/hermes-agent"
TARGETS = ["test_fresh_source_install", "test_kanban_work_obligations", "desktop_build_lock",
           "post_turn_hooks", "cron_run_wait_abandon"]


def gh(path, raw=False):
    r = subprocess.run(["gh", "api", "--allow-escape-sequences", path], capture_output=True, text=True, encoding="utf-8", timeout=180)
    if r.returncode:
        return None
    return r.stdout if raw else json.loads(r.stdout)


def main():
    runs = sys.argv[1:]
    fails = Counter()
    target_hits = defaultdict(list)
    for run in runs:
        jobs = gh(f"repos/{REPO}/actions/runs/{run}/jobs?per_page=100") or {"jobs": []}
        for j in jobs["jobs"]:
            if "slice" not in j["name"] and "e2e" not in j["name"]:
                continue
            log = gh(f"repos/{REPO}/actions/jobs/{j['id']}/logs", raw=True)
            if not log:
                print("LOGMISS", run, j["id"]); continue
            for line in log.splitlines():
                m = re.search(r"(FAILED|ERROR) (tests/\S+)", line)
                if m:
                    tid = m.group(2)
                    fails[tid.split("::")[0]] += 1
                for t in TARGETS:
                    if t in line and re.search(r"(PASSED|FAILED|ERROR|\d+\.\d+s)", line):
                        target_hits[t].append(f"{run} {j['conclusion']} {line.strip()[-160:]}")
    print("== failing files (count across runs)")
    for f, n in fails.most_common():
        print(n, f)
    print("== target lines")
    for t in TARGETS:
        print("--", t, len(target_hits[t]))
        for l in target_hits[t][:12]:
            print("  ", l)


if __name__ == "__main__":
    main()
