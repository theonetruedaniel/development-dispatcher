import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.dispatch.__main__ import main


class QualifyCli(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runtime=self.root/'runtime.json';self.runtime.write_text('{}')
        self.corpus=Path(__file__).parent/'fixtures/laya-routing-corpus.json'
        self.output=self.root/'result.json'
        self.args=['qualify-selector','--runtime',str(self.runtime),'--corpus',str(self.corpus),
                   '--output',str(self.output)]
        self.state=self.root/'state.json'
        self.enterContext(patch('scripts.dispatch.selector.SELECTOR_STATE_PATH',self.state))

    def test_existing_output_prevents_any_launch(self):
        self.output.write_text('preserve')
        with patch('scripts.dispatch.selector.run_batch') as run,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(self.args),2)
        run.assert_not_called()
        self.assertEqual(self.output.read_text(),'preserve')

    def test_completed_receipt_scores_warm_timings_without_admission(self):
        corpus=json.loads(self.corpus.read_text())
        proposals=[dict(status='ok',task_class=c['floor'],selector_revision='a'*40,
                        reason_code='classified') for c in corpus]
        receipt=dict(status='completed',termination_observed=True,proposals=proposals,
                     measurements={'cold_seconds':10,'warm_seconds':[.2]*len(corpus)})
        with patch('scripts.dispatch.selector.run_batch',return_value=receipt),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(self.args),0)
        result=json.loads(self.output.read_text())
        self.assertFalse(result['adoption_qualified'])
        self.assertEqual(result['evaluation']['p95_seconds'],.2)
        self.assertTrue(result['evaluation']['quality_pass'])
        self.assertFalse(self.state.exists())

    def test_uncertain_receipt_blocks_next_evaluation(self):
        with patch('scripts.dispatch.selector.run_batch',return_value=dict(status='uncertain',
                   termination_observed=False,proposals=[])) as run,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(self.args),2)
            self.assertTrue(self.state.exists())
            self.args[-1]=str(self.root/'second.json')
            self.assertEqual(main(self.args),2)
            run.assert_called_once()
