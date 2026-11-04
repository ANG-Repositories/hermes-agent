"""Recover actual per-file durations from the existing CI census; no new load."""
import concurrent.futures
import json
from pathlib import Path
import re
import subprocess
import time

TARGETS = (
    'test_fresh_source_install.py', 'test_kanban_work_obligations.py',
    'test_desktop_build_lock.py', 'test_post_turn_hooks_after_delivery.py',
    'test_cron_run_wait_abandon_signal.py',
)
ROOT = Path(__file__).resolve().parent / 't_a000_evidence'


def fetch(row):
    dst = ROOT / f"job-{row['jid']}.log"
    if not dst.exists():
        for attempt in range(3):
            p = subprocess.run(['gh', 'api', '--allow-escape-sequences',
                f"repos/ANG-Ventures/hermes-agent-ci/actions/jobs/{row['jid']}/logs"],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=90)
            if p.returncode == 0:
                dst.write_bytes(p.stdout)
                break
            time.sleep(2 * (attempt + 1))
        else:
            return [], dict(row, error=p.stderr.decode(errors='replace')[:300])
    text = dst.read_text(errors='replace')
    text = re.sub(r'\x1b\[[0-9;]*m', '', text)
    found = []
    for line in text.splitlines():
        for target in TARGETS:
            if target not in line:
                continue
            # Per-file runner completion line, not a failure dump or summary.
            m = re.search(r'\] ([✓✗]) (tests/\S+\.py) \((.*?)([\d.]+)s\)\s*$', line)
            if m and m[2].endswith('/' + target):
                found.append(dict(row, file=m[2], seconds=float(m[4]),
                                  passed=m[1] == '✓', raw=line))
    return found, None


def main():
    ROOT.mkdir(exist_ok=True)
    jobs = json.loads(Path('/tmp/t_a000_jobs.json').read_text())
    jobs = [j for j in jobs if (j['job'].startswith('Python tests / Run tests slice')
            or j['job'].startswith('Bootstrap installer / install.sh'))
            and j['host'] in ('ci-box', 'ace-ai', 'ace-media')]
    rows, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for n, (found, error) in enumerate(pool.map(fetch, jobs), 1):
            rows.extend(found)
            if error:
                errors.append(error)
            if n % 20 == 0:
                print(f'{n}/{len(jobs)} jobs; {len(rows)} duration rows; {len(errors)} API misses', flush=True)
    (ROOT / 'durations.json').write_text(json.dumps(rows, indent=2))
    (ROOT / 'api-errors.json').write_text(json.dumps(errors, indent=2))
    for target in TARGETS:
        for host in ('ci-box', 'ace-ai', 'ace-media'):
            rs = [r for r in rows if r['host'] == host and r['file'].endswith('/' + target)]
            vals = sorted(r['seconds'] for r in rs)
            good = sorted(r['seconds'] for r in rs if r['passed'])
            print(target, host, 'n=', len(vals), 'green=', len(good),
                  'median=', vals[len(vals)//2] if vals else None,
                  'range=', (min(vals), max(vals)) if vals else None,
                  'green_range=', (min(good), max(good)) if good else None)


if __name__ == '__main__':
    main()
