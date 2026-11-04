import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from t_a000_jobs import api


def main():
    r = 'ANG-Ventures/hermes-agent-ci'
    runs = api(f'repos/{r}/actions/workflows/388948865/runs?branch=main&per_page=100')[0]['workflow_runs'][:60]
    rows = []
    for i, run in enumerate(runs):
        for page in api(f"repos/{r}/actions/runs/{run['id']}/jobs?per_page=100&filter=latest"):
            for j in page['jobs']:
                if j['status'] != 'completed':
                    continue
                rows.append(dict(run=run['id'], job=j['name'], jid=j['id'],
                    host=(j.get('runner_name') or '').split('-ang')[0], concl=j['conclusion']))
        print(f'{i+1}/{len(runs)} {len(rows)} jobs', flush=True)
    Path('/tmp/t_a000_jobs.json').write_text(json.dumps(rows))


if __name__ == '__main__':
    main()
