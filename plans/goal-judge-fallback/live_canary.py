"""Manual, non-mutating live fallback canary. Uses the caller's configured Sol alias."""
# SCHEDULE: manual (operator/worker acceptance only)
import json
import logging


def main():
    from hermes_cli.config import load_config
    from hermes_cli.goal_judge_chain import judge_chain
    from hermes_cli.goals import _parse_judge_response
    alias = load_config()['model']['aliases']['sol']
    provider, model = alias.split('/', 1)
    logging.basicConfig(level=logging.INFO)
    routes = [
        dict(provider='custom', model='unreachable-control', base_url='http://127.0.0.1:9/v1', api_key='unused', timeout=10),
        dict(provider=provider, model=model, timeout=60, reasoning_effort='medium'),
    ]
    result = judge_chain((routes, 75), 'Return only a JSON object with verdict and reason. '
                         'Use verdict continue when the stated deliverable does not exist.',
                         'Goal: write artifact. Evidence: artifact not written yet.',
                         4096, _parse_judge_response)
    print(json.dumps(dict(verdict=result[0], parse_failed=result[2], transport_failed=result[4])))
    if result[0] != 'continue' or result[2] or result[4]:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
