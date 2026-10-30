"""Opt-in automatic delivery is bounded and never repeats an uncertain attempt."""
import threading
import time
import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import unittest

from stripe_double.http_api import SimulatorServer
from stripe_double.harness import StripeDouble
from stripe_double.wire import SimulatorError
from test_state import FIXTURE, case_path
import test_http


class AutomaticWebhookTests(unittest.TestCase):
    setUp = test_http.HttpTests.setUp
    close = test_http.HttpTests.close
    request = test_http.HttpTests.request
    account = test_http.HttpTests.account
    admin = test_http.HttpTests.admin

    def activate(self):
        return self.request('POST',f'/__simulator/v1/runs/{self.run}/webhooks/start',{},
                            token=self.server.credentials.admin_token,json_body=True)

    def test_automatic_delivery_is_opt_in_and_failed_attempt_is_not_retried(self):
        received = []
        arrived = threading.Event()
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                received.append(inner.rfile.read(int(inner.headers['Content-Length'])))
                inner.send_response(500)
                inner.send_header('Content-Length','0')
                inner.end_headers()
                arrived.set()
            def log_message(inner,*args):
                pass
        receiver = ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        thread = threading.Thread(target=receiver.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(thread.join,3)
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        self.server.close()
        self.server = SimulatorServer(self.store,clock=self.clock,
            callback=f'http://127.0.0.1:{receiver.server_port}/event',automatic_webhooks=True).start()
        self.assertEqual(self.activate()[0],200)
        self.account()
        self.assertTrue(arrived.wait(3))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            event = self.store.events(self.run)[0]
            if event['delivery_state'] == 'failed':
                break
            threading.Event().wait(.01)
        self.assertEqual(event['delivery_state'],'failed')
        self.assertEqual(event['attempts'],1)
        self.assertFalse(threading.Event().wait(.35))
        self.assertEqual(len(received),1)
        self.assertEqual(self.store.events(self.run)[0]['attempts'],1)

    def test_automatic_delivery_requires_callback(self):
        with self.assertRaises(SimulatorError):
            SimulatorServer(self.store,clock=self.clock,automatic_webhooks=True)

    def test_configured_retry_preserves_event_bytes_and_observable_schedule(self):
        received = []
        arrived = threading.Event()
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                received.append(inner.rfile.read(int(inner.headers['Content-Length'])))
                inner.send_response(500 if len(received) == 1 else 200)
                inner.send_header('Content-Length','0')
                inner.end_headers()
                arrived.set()
            def log_message(inner,*args):
                pass
        receiver = ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        thread = threading.Thread(target=receiver.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(thread.join,3)
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        self.server.close()
        self.server = SimulatorServer(self.store,clock=self.clock,
            callback=f'http://127.0.0.1:{receiver.server_port}/event',automatic_webhooks=True,
            webhook_max_attempts=3,webhook_retry_delay=1).start()
        self.assertEqual(self.activate()[0],200)
        self.account()
        self.assertTrue(arrived.wait(3))
        arrived.clear()
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            page = self.request('POST',f'/__simulator/v1/runs/{self.run}/events/list',{},
                token=self.server.credentials.admin_token,json_body=True)[1]
            if page['events'][0]['delivery_state'] != 'sending':
                break
            threading.Event().wait(.01)
        self.assertEqual(page['events'][0]['delivery_state'],'retry_scheduled')
        self.assertEqual(page['events'][0]['attempts'],1)
        self.assertEqual(page['events'][0]['next_retry_at'],int(self.clock())+1)
        self.assertFalse(arrived.wait(.2))
        self.clock.advance(1)
        self.assertTrue(arrived.wait(3))
        self.assertEqual(received[0],received[1])

    def test_scheduled_retry_survives_restart_and_requires_activation(self):
        received = []
        arrived = threading.Event()
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                received.append(inner.rfile.read(int(inner.headers['Content-Length'])))
                inner.send_response(500 if len(received) == 1 else 200)
                inner.send_header('Content-Length','0')
                inner.end_headers()
                arrived.set()
            def log_message(inner,*args):
                pass
        receiver = ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        thread = threading.Thread(target=receiver.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(thread.join,3)
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        self.server.close()
        callback = f'http://127.0.0.1:{receiver.server_port}/event'
        self.server = SimulatorServer(self.store,clock=self.clock,callback=callback,automatic_webhooks=True,
            webhook_max_attempts=2,webhook_retry_delay=1).start()
        self.assertEqual(self.activate()[0],200)
        self.account()
        self.assertTrue(arrived.wait(3))
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            if self.store.events(self.run)[0]['delivery_state'].startswith('retry:'):
                break
            threading.Event().wait(.01)
        self.server.close()
        self.store.close()
        self.store = test_http.StateStore(self.path,clock=self.clock)
        self.clock.advance(1)
        arrived.clear()
        self.server = SimulatorServer(self.store,clock=self.clock,callback=callback,automatic_webhooks=True,
            webhook_max_attempts=2,webhook_retry_delay=1).start()
        self.assertFalse(arrived.wait(.3))
        self.assertEqual(self.activate()[0],200)
        self.assertTrue(arrived.wait(3))
        self.assertEqual(received[0],received[1])

    def test_manual_delivery_can_recover_after_automatic_attempt_limit(self):
        received = []
        arrived = threading.Event()
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                received.append(inner.rfile.read(int(inner.headers['Content-Length'])))
                inner.send_response(500 if len(received) <= 2 else 200)
                inner.send_header('Content-Length','0')
                inner.end_headers()
                arrived.set()
            def log_message(inner,*args):
                pass
        receiver = ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        thread = threading.Thread(target=receiver.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(thread.join,3)
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        self.server.close()
        self.server = SimulatorServer(self.store,clock=self.clock,
            callback=f'http://127.0.0.1:{receiver.server_port}/event',automatic_webhooks=True,
            webhook_max_attempts=2,webhook_retry_delay=1).start()
        self.assertEqual(self.activate()[0],200)
        self.account()
        self.assertTrue(arrived.wait(3))
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            if self.store.events(self.run)[0]['delivery_state'].startswith('retry:'):
                break
            threading.Event().wait(.01)
        arrived.clear()
        self.clock.advance(1)
        self.assertTrue(arrived.wait(3))
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            event = self.store.events(self.run)[0]
            if event['delivery_state'] == 'failed':
                break
            threading.Event().wait(.01)
        self.assertEqual((event['delivery_state'],event['attempts']),('failed',2))
        status, result, _ = self.request('POST',f'/__simulator/v1/runs/{self.run}/events/deliver',
            {'delivery_id':event['delivery_id'],'expected_state_version':self.store.run(self.run)['state_version']},
            token=self.server.credentials.admin_token,json_body=True)
        self.assertEqual(status,200,result)
        self.assertTrue(result['delivery']['delivered'])
        self.assertEqual(result['delivery']['attempts'],3)
        self.assertEqual(received[0],received[2])

    def test_lower_retry_limit_after_restart_marks_schedule_exhausted(self):
        arrived = threading.Event()
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                inner.rfile.read(int(inner.headers['Content-Length']))
                inner.send_response(500)
                inner.send_header('Content-Length','0')
                inner.end_headers()
                arrived.set()
            def log_message(inner,*args):
                pass
        receiver = ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        thread = threading.Thread(target=receiver.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(thread.join,3)
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        self.server.close()
        callback = f'http://127.0.0.1:{receiver.server_port}/event'
        self.server = SimulatorServer(self.store,clock=self.clock,callback=callback,automatic_webhooks=True,
            webhook_max_attempts=3,webhook_retry_delay=60).start()
        self.assertEqual(self.activate()[0],200)
        self.account()
        self.assertTrue(arrived.wait(3))
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            if self.store.events(self.run)[0]['delivery_state'].startswith('retry:'):
                break
            threading.Event().wait(.01)
        self.server.close()
        self.store.close()
        self.store = test_http.StateStore(self.path,clock=self.clock)
        self.server = SimulatorServer(self.store,clock=self.clock,callback=callback,automatic_webhooks=True,
            webhook_max_attempts=1).start()
        self.assertEqual(self.activate()[0],200)
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            event = self.store.events(self.run)[0]
            if event['delivery_state'] == 'failed':
                break
            threading.Event().wait(.01)
        self.assertEqual((event['delivery_state'],event['attempts']),('failed',1))

    def test_manual_attempt_preserves_existing_automatic_retry_policy(self):
        received = []
        arrived = threading.Event()
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                received.append(inner.rfile.read(int(inner.headers['Content-Length'])))
                inner.send_response(500 if len(received) < 3 else 200)
                inner.send_header('Content-Length','0')
                inner.end_headers()
                arrived.set()
            def log_message(inner,*args):
                pass
        receiver = ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        thread = threading.Thread(target=receiver.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(thread.join,3)
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        self.server.close()
        self.server = SimulatorServer(self.store,clock=self.clock,
            callback=f'http://127.0.0.1:{receiver.server_port}/event',automatic_webhooks=True,
            webhook_max_attempts=3,webhook_retry_delay=1).start()
        self.assertEqual(self.activate()[0],200)
        self.account()
        self.assertTrue(arrived.wait(3))
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            event = self.store.events(self.run)[0]
            if event['delivery_state'].startswith('retry:'):
                break
            threading.Event().wait(.01)
        status, result, _ = self.request('POST',f'/__simulator/v1/runs/{self.run}/events/deliver',
            {'delivery_id':event['delivery_id'],'expected_state_version':self.store.run(self.run)['state_version']},
            token=self.server.credentials.admin_token,json_body=True)
        self.assertEqual(status,200,result)
        self.assertFalse(result['delivery']['delivered'])
        page = self.request('POST',f'/__simulator/v1/runs/{self.run}/events/list',{},
            token=self.server.credentials.admin_token,json_body=True)[1]
        self.assertEqual(page['events'][0]['delivery_state'],'retry_scheduled')
        self.assertEqual(page['events'][0]['next_retry_at'],int(self.clock())+2)
        arrived.clear()
        self.clock.advance(2)
        self.assertTrue(arrived.wait(3))
        self.assertEqual(len(received),3)
        self.assertEqual(received[0],received[2])

    def test_process_delivers_signed_event_and_joins(self):
        received = []
        arrived = threading.Event()
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                payload = inner.rfile.read(int(inner.headers['Content-Length']))
                received.append((payload,inner.headers['Stripe-Signature']))
                inner.send_response(200)
                inner.send_header('Content-Length','0')
                inner.end_headers()
                arrived.set()
            def log_message(inner,*args):
                pass
        receiver = ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        thread = threading.Thread(target=receiver.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(thread.join,3)
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        root = Path(__file__).resolve().parents[1]
        with StripeDouble(root,case_path(self),callback=f'http://127.0.0.1:{receiver.server_port}/event',
                          automatic_webhooks=True) as sim:
            self.assertEqual(sim.start_automatic_webhooks()[0],200)
            status, account = sim.request('POST','/v1/accounts',form=FIXTURE['account_form'],key='auto-account')
            self.assertEqual(status,200,account)
            self.assertTrue(arrived.wait(3))
            payload, signature = received[0]
            timestamp, digest = (part.split('=',1)[1] for part in signature.split(','))
            expected = hmac.new(sim.webhook_secret.encode(),timestamp.encode()+b'.'+payload,hashlib.sha256).hexdigest()
            self.assertTrue(hmac.compare_digest(digest,expected))
            self.assertEqual(json.loads(payload)['data']['object']['id'],account['id'])
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status, events = sim.list_events()
                if events['events'][0]['delivery_state'] == 'delivered':
                    break
                threading.Event().wait(.01)
            self.assertEqual(events['events'][0]['delivery_state'],'delivered')
            self.assertEqual(events['events'][0]['attempts'],1)
        self.assertTrue(sim.closed)
        self.assertEqual(len(received),1)

    def test_uncertain_event_is_not_replayed_automatically_after_restart(self):
        self.account('before-restart')
        reserved = self.store.reserve_event(self.run)
        self.assertEqual(reserved['attempts'],1)
        received = []
        arrived = threading.Event()
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                received.append(json.loads(inner.rfile.read(int(inner.headers['Content-Length']))))
                inner.send_response(200)
                inner.send_header('Content-Length','0')
                inner.end_headers()
                arrived.set()
            def log_message(inner,*args):
                pass
        receiver = ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        thread = threading.Thread(target=receiver.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(thread.join,3)
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        self.server.close()
        self.store.close()
        self.store = test_http.StateStore(self.path,clock=self.clock)
        self.server = SimulatorServer(self.store,clock=self.clock,
            callback=f'http://127.0.0.1:{receiver.server_port}/event',automatic_webhooks=True).start()
        self.assertEqual(self.activate()[0],200)
        second = self.account('after-restart')
        self.assertTrue(arrived.wait(3))
        self.assertEqual(received[0]['data']['object']['id'],second)
        first = self.store.events(self.run)[0]
        self.assertEqual((first['delivery_state'],first['attempts']),('sending',1))

    def test_restart_waits_for_consumer_to_install_rotated_secret(self):
        received = []
        arrived = threading.Event()
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                received.append((inner.rfile.read(int(inner.headers['Content-Length'])),inner.headers['Stripe-Signature']))
                inner.send_response(200)
                inner.send_header('Content-Length','0')
                inner.end_headers()
                arrived.set()
            def log_message(inner,*args):
                pass
        receiver = ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        thread = threading.Thread(target=receiver.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(thread.join,3)
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        root, state = Path(__file__).resolve().parents[1], case_path(self)
        with StripeDouble(root,state) as first:
            self.assertEqual(first.request('POST','/v1/accounts',form=FIXTURE['account_form'],key='queued')[0],200)
            instance, run, old_secret = first.instance_id, first.run_id, first.webhook_secret
        with StripeDouble(root,state,create=False,instance=instance,run=run,
                          callback=f'http://127.0.0.1:{receiver.server_port}/event',automatic_webhooks=True) as restarted:
            self.assertNotEqual(restarted.webhook_secret,old_secret)
            self.assertFalse(arrived.wait(.35))
            self.assertEqual(restarted.list_events()[1]['events'][0]['attempts'],0)
            installed_secret = restarted.webhook_secret
            self.assertEqual(restarted.start_automatic_webhooks()[0],200)
            self.assertTrue(arrived.wait(3))
            payload, signature = received[0]
            timestamp, digest = (part.split('=',1)[1] for part in signature.split(','))
            expected = hmac.new(installed_secret.encode(),timestamp.encode()+b'.'+payload,hashlib.sha256).hexdigest()
            self.assertTrue(hmac.compare_digest(digest,expected))
