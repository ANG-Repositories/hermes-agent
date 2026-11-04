import json
from pathlib import Path
import statistics


def main():
    root = Path(__file__).resolve().parent
    rows = json.loads((root / 't_a000_evidence/durations.json').read_text())
    out = ['t_a00081df measurement receipt', '',
           'Source: 60 latest main CI runs on ANG-Ventures/hermes-agent-ci.',
           '59 run job inventories recovered; 1 exhausted HTTP 502 retries.',
           '1003 selected job logs; 15 API misses; 324 parsed per-file completion rows.',
           'File wall durations, NOT individual-test durations. Reds are censored by harness caps.',
           'Repeated file appearances across jobs are separate observations, not necessarily distinct runs.',
           'These measurements do NOT establish >=10 per-test runs on every venue.', '',
           'file | host | observations | distinct runs | green | p50 seconds | min..max seconds']
    for filename in sorted({r['file'] for r in rows}):
        for host in ('ci-box','ace-ai','ace-media'):
            rs=[r for r in rows if r['file']==filename and r['host']==host]
            if rs:
                vals=[r['seconds'] for r in rs]
                out.append(f"{filename} | {host} | {len(rs)} | {len({r['run'] for r in rs})} | {sum(r['passed'] for r in rs)} | {statistics.median(vals):.2f} | {min(vals)}..{max(vals)}")
    out += ['', 'Verified changes (no merges performed):',
        '#2577 b214a1b1a09b4cb18ddf986ad9357f9a2906a3b6: fresh install slow_test(200), operator-approved budget.',
        'ci-box managed Python3.14.7, affinity4: 3 passed in108.93s; missing-wheel62.35s, None28.45s, bad-hash16.74s.',
        'PR CI41750491151: all4 Python slices success. Slice4 job126316547217: fresh install3 passed,42.7s.',
        '#2579 f76edc4c6146aabcca968dfe7ed1bf32a0932e91: text=True beside encoding (scanner false positive).',
        'Footguns:1 before;0 after across2227 files. Related tests21 passed in29.21s. Ruff passes.',
        '#2580 db40990de3473c61b688294f0c055f15eebd6ff9: synchronous goal fixture DB warm-up.',
        'Baseline hooks+cron:3 failed,32 passed in169.29s; failed goal writes exceed cold-bootstrap window.',
        'After whole hooks file:29 passed in65.86s. No budget or production change.',
        'Unchanged probes: obligations CLI51.66s; desktop failure73.37s;2 passed in127.23s.',
        '', 'Remaining:',
        'No green universal-ci main run. Mirror main52f53b68 is stale versus source58a5d53b.',
        'Main run41678439512 has35 ci-priv jobs queued; runner-admin API403 for worker.',
        'Cutover card t_868e850a is retiring this mirror; acceptance destination requires operator reconciliation.',
        'Need complete per-node >=10-run measurements for all3 venues before any further budget decision.',
        'No placement changes; no budgets changed except operator-approved fresh install.',
        'deviations: none (affinity only scoped to test subprocesses).']
    (root / 't_a000_receipt.txt').write_text('\n'.join(out)+'\n')
    print('\n'.join(out))


if __name__ == '__main__':
    main()
