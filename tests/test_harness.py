"""A separate Python consumer exercises the actual external app and ownership."""
from pathlib import Path
import http.client
import json
from unittest.mock import patch
import unittest

from stripe_double.harness import StripeDouble, TransportUnknown
from test_state import FIXTURE, case_path

ROOT = Path(__file__).resolve().parents[1]


class HarnessTests(unittest.TestCase):
    def test_consumer_can_run_a_local_account_and_control_it(self):
        state = case_path(self)
        with StripeDouble(ROOT, state) as sim:
            self.assertNotIn('sk_test_', repr(sim))
            status, account = sim.request('POST', '/v1/accounts', form=FIXTURE['account_form'], key='account')
            self.assertEqual(status, 200, account)
            status, run = sim.inspect_run()
            self.assertEqual(status, 200, run)
            status, run = sim.action('requirements_complete', run['state_version'])
            self.assertEqual(status, 200, run)
            status, current = sim.request('GET', '/v1/accounts/' + account['id'])
            self.assertEqual(status, 200, current)
            self.assertTrue(current['charges_enabled'])
            self.assertFalse(sim.closed)
        self.assertTrue(sim.closed)
        self.assertTrue(state.is_file())
        with self.assertRaises(RuntimeError):
            sim.inspect_run()

    def test_lost_reply_is_unknown_without_automatic_mutation_replay(self):
        state = case_path(self)
        with StripeDouble(ROOT, state) as sim:
            status, run = sim.inspect_run()
            self.assertEqual(status, 200, run)
            status, _ = sim.action('drop_next_reply_after_commit', run['state_version'])
            self.assertEqual(status, 200)
            with self.assertRaises(TransportUnknown):
                sim.request('POST', '/v1/accounts', form=FIXTURE['account_form'], key='original')
            status, events = sim.list_events()
            self.assertEqual(status, 200, events)
            self.assertEqual(len(events['events']), 1)
            status, account = sim.request('POST', '/v1/accounts', form=FIXTURE['account_form'], key='original')
            self.assertEqual(status, 200, account)
            self.assertEqual(len(sim.list_events()[1]['events']), 1)


    def test_interrupt_during_response_closes_stream_and_preserves_identity(self):
        state = case_path(self)
        responses = []
        with StripeDouble(ROOT, state) as sim:
            def interrupt(response, *args, **kwargs):
                responses.append(response)
                raise KeyboardInterrupt('original interrupt')
            with patch.object(http.client.HTTPResponse, 'read', interrupt):
                with self.assertRaisesRegex(KeyboardInterrupt, 'original interrupt'):
                    sim.request('POST', '/v1/accounts', form=FIXTURE['account_form'], key='interrupted')
            self.assertTrue(responses[0].isclosed())
            status, events = sim.list_events()
            self.assertEqual(status,200,events)
            self.assertEqual(len(events['events']),1)
            self.assertEqual(events['events'][0]['type'],'account.updated')

    def test_bootstrap_rejects_remote_destinations_and_real_credentials(self):
        state = case_path(self)
        with StripeDouble(ROOT, state) as sim:
            original = dict(sim._bootstrap)
            for key, value in [('origin','http://example.org:80'),('api_key','sk_test_real'),
                               ('scenario_id','not-a-scenario'),('schema_version',True)]:
                with self.subTest(key=key):
                    invalid = dict(original)
                    invalid[key] = value
                    with self.assertRaises(RuntimeError):
                        StripeDouble._validate_bootstrap(json.dumps(invalid).encode())


if __name__ == '__main__':
    unittest.main()
