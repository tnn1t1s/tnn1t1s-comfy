import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from tnn1t1s_comfy.client import APIError, Cue, doctor, explain


class CueContracts(unittest.TestCase):
    def test_cancel_carries_identity_and_waits_past_acceptance(self):
        cue = Cue('http://example.test')
        with patch.object(cue, 'request', return_value={'status': 'running'}) as request:
            self.assertEqual(cue.cancel('render/one', 'team:comfy')['status'], 'running')
            request.assert_called_once_with('/jobs/render%2Fone', {'submitter': 'team:comfy'}, 'DELETE')
        with patch.object(cue, 'job', side_effect=[{'status': 'running'}, {'status': 'cancelled'}]):
            self.assertEqual(cue.wait('render/one', poll=0)['status'], 'cancelled')

    def test_timeout_does_not_claim_killed(self):
        cue = Cue('http://example.test')
        with patch.object(cue, 'job', return_value={'status': 'running'}):
            with self.assertRaisesRegex(TimeoutError, 'may still be running'):
                cue.wait('render', timeout=0)

    def test_cancel_terminal_race_reports_actual_outcome(self):
        cue = Cue('http://example.test')
        with patch.object(cue, 'request', side_effect=APIError(409, 'terminal')), \
             patch.object(cue, 'job', return_value={'status': 'succeeded'}):
            self.assertEqual(cue.cancel('render', 'team:comfy')['status'], 'succeeded')

    def test_duplicate_submission_checks_identity(self):
        cue = Cue('http://example.test')
        spec = {'id': 'render', 'command': 'render graph-a', 'submitter': 'team:comfy'}
        with patch.object(cue, 'request', side_effect=APIError(409, {})), \
             patch.object(cue, 'job', return_value={**spec, 'status': 'running'}):
            self.assertEqual(cue.submit(spec)['status'], 'running')
        with patch.object(cue, 'request', side_effect=APIError(409, {})), \
             patch.object(cue, 'job', return_value={**spec, 'command': 'render graph-b'}):
            with self.assertRaisesRegex(ValueError, 'different specification'):
                cue.submit(spec)

    def test_missing_pending_reason_stays_unknown(self):
        result = explain({'id': 'r', 'status': 'pending'})
        self.assertIsNone(result['reason_code'])
        self.assertIn('not yet established', result['reason'])

    def test_actual_pending_reason_is_preserved(self):
        result = explain({'id': 'r', 'status': 'pending', 'reason_code': 'waiting_store',
                          'reason': 'waiting for a node that declares store models'})
        self.assertEqual(result['reason_code'], 'waiting_store')
        self.assertIn('models', result['reason'])

    def test_doctor_reports_busy_queue_without_claiming_cuda(self):
        with patch('tnn1t1s_comfy.client.API.request', side_effect=[{}, {'queue_running': [1], 'queue_pending': []}]):
            result = doctor('http://example.test')
            self.assertTrue(result['ok'])
            self.assertEqual(result['checks'][1]['detail']['running'], 1)
            self.assertIn('does not test', result['checks'][0]['detail'])

    def test_doctor_missing_custom_nodes_fails(self):
        with patch('tnn1t1s_comfy.client.API.request', side_effect=[{}, {'queue_running': [], 'queue_pending': []}, {}]):
            self.assertFalse(doctor('http://example.test', required_nodes=['WanNode'])['ok'])


class FennelDiscovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('fennel_url', Path(__file__).parents[1] / 'deploy/fennel_url.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_scheduled_instance_does_not_return_stale_url(self):
        with self.assertRaises(ValueError):
            self.module.resolve({'instances': [{'goal': 'Running', 'condition': 'Scheduled',
                'health': 'Healthy', 'agent_info': {'host': 'stale'}, 'ports': [8188]}]})

    def test_uses_current_assigned_port(self):
        self.assertEqual(self.module.resolve({'instances': [{'goal': 'Running', 'condition': 'Running',
            'health': 'Healthy', 'agent_info': {'host': 'worker.test'}, 'ports': [31042]}]}),
            'http://worker.test:31042')


if __name__ == '__main__':
    unittest.main()
