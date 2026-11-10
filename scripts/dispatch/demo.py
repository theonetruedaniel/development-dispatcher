"""Five synthetic CLI previews; no model calls, accounts or live activation."""
import contextlib
import io
import json
from pathlib import Path
import tempfile

from .__main__ import main as preview_main


def run_demo():
    # Expected outcomes are fixed independently of the policy's routing table.
    scenarios = [
        ('Summarize a synthetic task list', 'routine', None, False,
         'gpt-6-luna', 'low', 'dispatch'),
        ('Review a consequential change', 'consequential', None, False,
         'gpt-6-astra', 'high', 'dispatch'),
        ('Format the next synthetic checklist', 'routine', None, False,
         'gpt-6-luna', 'low', 'dispatch'),
        ('Honor an explicit Manual selection', 'routine',
         {'model': 'gpt-6-sol', 'effort': 'high'}, False,
         'gpt-6-sol', 'high', 'dispatch'),
        ('Stop when that Manual route is unavailable', 'routine',
         {'model': 'gpt-6-sol', 'effort': 'high'}, True,
         'gpt-6-sol', 'high', 'blocked'),
    ]
    rows = []
    with tempfile.TemporaryDirectory(prefix='abrams-offline-demo-') as directory:
        root = Path(directory)
        for index, (label, kind, manual, unavailable, model, effort, action) in enumerate(scenarios, 1):
            manifest = {
                'schema_version': 1, 'package_id': f'offline-demo-{index}',
                'task': 'Return exactly OK. Synthetic preview only.',
                'task_class': kind, 'workspace': str(root),
                'allowed_actions': ['read'], 'required': True,
                'timeout_seconds': 60, 'max_attempts': 1,
                'verification': [{'type': 'final_equals', 'expected': 'OK'}],
            }
            if manual:
                manifest.update(mode='manual', manual_route=manual)
            catalog = {'gpt-6-luna': ['low'], 'gpt-6-sol': ['high'],
                       'gpt-6-astra': ['high']}
            if unavailable:
                del catalog['gpt-6-sol']
            work_path, catalog_path = root / 'work.json', root / 'catalog.json'
            work_path.write_text(json.dumps(manifest), encoding='utf-8')
            catalog_path.write_text(json.dumps(catalog), encoding='utf-8')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = preview_main(['preview', '--manifest', str(work_path),
                                     '--catalog', str(catalog_path)])
            result = json.loads(output.getvalue())
            decision = result.get('decision', {})
            route = decision.get('route', {})
            passed = (route == {'model': model, 'effort': effort}
                      and decision.get('action') == action
                      and decision.get('usage_band') == 'unknown'
                      and result.get('dispatched') is False
                      and result.get('evidence') == 'offline preview; supplied catalog not live verified'
                      and code == (2 if action == 'blocked' else 0))
            rows.append({'scenario': label, 'model': route.get('model'),
                         'effort': route.get('effort'), 'action': decision.get('action'),
                         'reason': decision.get('reason'),
                         'usage_band': decision.get('usage_band'),
                         'dispatched': result.get('dispatched'), 'passed': passed})
    return rows


def main():
    print('Abrams Intelligence | offline development dispatcher demo')
    print('Synthetic tasks and catalog. No model calls. Account usage: unknown.\n')
    try:
        rows = run_demo()
    except (OSError, ValueError, TypeError) as exc:
        print(f'Demo could not complete: {type(exc).__name__}')
        return 2
    for index, row in enumerate(rows, 1):
        label = 'PASS' if row['passed'] else 'FAIL'
        print(f"{index}. {row['scenario']}")
        print(f"   {row['model']} / {row['effort']} | policy: {row['action']} | {label}")
    count = sum(row['passed'] for row in rows)
    print(f'\n{count}/{len(rows)} checks passed. Every preview must report dispatched=false.')
    print('A dispatch decision is a recommendation only; no task was executed.')
    print('Live dispatch and Laya remain unadmitted on the current build.')
    return 0 if count == len(rows) else 2


if __name__ == '__main__':
    raise SystemExit(main())
