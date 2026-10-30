"""Near-term observable provider behaviors for local application tests."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest

from stripe_double.http_api import SimulatorServer
from test_state import FIXTURE
import test_http


class NearTermHttpTests(unittest.TestCase):
    setUp = test_http.HttpTests.setUp
    close = test_http.HttpTests.close
    request = test_http.HttpTests.request
    admin = test_http.HttpTests.admin
    account = test_http.HttpTests.account
    ready = test_http.HttpTests.ready
    session = test_http.HttpTests.session
    exchange = test_http.HttpTests.exchange
    component = test_http.HttpTests.component
    payment = test_http.HttpTests.payment

    def control(self, suffix, body, token=None):
        return self.request('POST', f'/__simulator/v1/runs/{self.run}/{suffix}', body,
                            token=token or self.server.credentials.admin_token, json_body=True)

    def grant(self, account, intent):
        status, obj, _ = self.request('GET', '/v1/payment_intents/' + intent, account=account)
        self.assertEqual(status, 200, obj)
        status, grant, _ = self.exchange(obj['client_secret'], account, 'card_payment')
        self.assertEqual(status, 200, grant)
        return grant

    def test_standard_confirmation_supports_platform_automatic_capture_and_replay(self):
        form = {'amount':'1000','currency':'usd','allowed_payment_method_types[]':'card'}
        status, intent, _ = self.request('POST','/v1/payment_intents',form,key='platform-intent')
        self.assertEqual(status,200,intent)
        self.assertEqual(intent['capture_method'],'automatic')
        endpoint = '/v1/payment_intents/' + intent['id'] + '/confirm'
        status, captured, _ = self.request('POST',endpoint,{'payment_method':'pm_card_visa'},key='confirm')
        self.assertEqual(status,200,captured)
        self.assertEqual(captured['status'],'succeeded')
        self.assertEqual(captured['amount_received'],1000)
        self.assertEqual(captured['latest_charge']['amount_captured'],1000)
        self.assertEqual(self.request('POST',endpoint,{'payment_method':'pm_card_visa'},key='confirm')[1],captured)
        charges = self.request('GET','/v1/charges?payment_intent=' + intent['id'])[1]
        self.assertEqual(len(charges['data']),1)
        self.assertEqual(self.request('POST',endpoint,{'payment_method':'pm_card_visa'},key='second')[0],409)

    def test_standard_confirmation_returns_a_decline_without_creating_a_charge(self):
        status, intent, _ = self.request('POST','/v1/payment_intents',
            {'amount':'1000','currency':'usd'},key='declined-intent')
        self.assertEqual(status,200,intent)
        self.assertEqual(self.admin('decline_next_confirmation')[0],200)
        endpoint = '/v1/payment_intents/' + intent['id'] + '/confirm'
        status, error, _ = self.request('POST',endpoint,{'payment_method':'pm_card_visa'},key='declined-confirm')
        self.assertEqual(status,402,error)
        self.assertEqual(error['error']['code'],'card_declined')
        self.assertEqual(error['error']['payment_intent']['status'],'requires_payment_method')
        self.assertEqual(self.request('POST',endpoint,{'payment_method':'pm_card_visa'},key='declined-confirm')[1],error)
        self.assertEqual(self.request('GET','/v1/charges?payment_intent=' + intent['id'])[1]['data'],[])

    def test_authentication_and_processing_have_explicit_recoverable_outcomes(self):
        account = self.account()
        self.ready()
        for trigger, intermediate, finish in [('require_authentication_next', 'requires_action', 'complete_authentication'),
                                               ('process_next_confirmation', 'processing', 'complete_processing')]:
            with self.subTest(trigger=trigger):
                status, state, _ = self.admin(trigger)
                self.assertEqual(status, 200, state)
                status, intent, _ = self.request('POST', '/v1/payment_intents', FIXTURE['intent_form'],
                                                 account=account, key=trigger)
                self.assertEqual(status, 200, intent)
                grant = self.grant(account, intent['id'])
                status, changed, _ = self.component(grant['control_capability'], account, 'card_payment',
                                                    'confirm', grant['provider_version'])
                self.assertEqual(status, 200, changed)
                status, observed, _ = self.request('GET', '/v1/payment_intents/' + intent['id'], account=account)
                self.assertEqual(observed['status'], intermediate)
                status, _, _ = self.request('POST', '/v1/payment_intents/' + intent['id'] + '/capture',
                    {'amount_to_capture':'1000'}, account=account, key='too-early-' + trigger)
                self.assertEqual(status, 409)
                status, changed, _ = self.component(grant['control_capability'], account, 'card_payment',
                                                    finish, changed['provider_version'])
                self.assertEqual(status, 200, changed)
                status, observed, _ = self.request('GET', '/v1/payment_intents/' + intent['id'], account=account)
                self.assertEqual(observed['status'], 'requires_capture')
                self.assertEqual(self.component(grant['control_capability'], account, 'card_payment',
                                                finish, changed['provider_version'])[0], 409)

    def test_failed_refund_releases_reservation_without_touching_other_refunds(self):
        account, intent = self.payment()
        status, captured, _ = self.request('POST', '/v1/payment_intents/' + intent + '/capture',
            FIXTURE['capture_form'], account=account, key='capture')
        self.assertEqual(status, 200, captured)
        charge = captured['latest_charge']['id']
        self.assertEqual(self.admin('refund_pending')[0], 200)
        status, refund, _ = self.request('POST', '/v1/refunds',
            {'charge':charge, 'amount':'600', 'refund_application_fee':'false'}, account=account, key='refund')
        self.assertEqual(status, 200, refund)
        status, other, _ = self.request('POST', '/v1/refunds',
            {'charge':charge, 'amount':'100', 'refund_application_fee':'false'}, account=account, key='other-refund')
        self.assertEqual(status, 200, other)
        status, info, _ = self.control('resources/inspect', {'kind':'refund','resource_id':refund['id'],'account':account})
        self.assertEqual(status, 200, info)
        status, changed, _ = self.control('refunds/' + refund['id'] + '/actions',
            {'action':'fail','account':account,'expected_provider_version':info['provider_version']})
        self.assertEqual(status, 200, changed)
        status, observed, _ = self.request('GET', '/v1/refunds/' + refund['id'], account=account)
        self.assertEqual(observed['status'], 'failed')
        self.assertEqual(observed['failure_reason'], 'declined')
        status, replacement, _ = self.request('POST', '/v1/refunds',
            {'charge':charge,'amount':'900','refund_application_fee':'false'}, account=account, key='replacement')
        self.assertEqual(status, 200, replacement)
        self.assertEqual(self.request('GET', '/v1/refunds/' + other['id'], account=account)[1]['status'], 'pending')
        self.assertEqual(self.request('POST', '/v1/refunds',
            {'charge':charge,'amount':'1','refund_application_fee':'false'}, account=account, key='excess')[0],409)
        self.assertEqual(self.control('refunds/' + refund['id'] + '/actions',
            {'action':'fail','account':account,'expected_provider_version':info['provider_version']})[0], 409)
        self.assertIn('refund.failed', [json.loads(bytes(row['payload']))['type'] for row in self.store.events(self.run)])

    def test_account_readiness_can_change_without_affecting_another_account(self):
        first = self.account('first')
        second = self.account('second')
        self.ready()
        status, info, _ = self.control('resources/inspect', {'kind':'account','resource_id':first,'account':first})
        self.assertEqual(status, 200, info)
        status, result, _ = self.control('accounts/' + first + '/readiness',
            {'action':'disable_payouts','expected_provider_version':info['provider_version']})
        self.assertEqual(status, 200, result)
        first_state = self.request('GET','/v1/accounts/' + first)[1]
        second_state = self.request('GET','/v1/accounts/' + second)[1]
        self.assertTrue(first_state['charges_enabled'])
        self.assertFalse(first_state['payouts_enabled'])
        self.assertTrue(second_state['charges_enabled'] and second_state['payouts_enabled'])
        status, intent, _ = self.request('POST','/v1/payment_intents',FIXTURE['intent_form'],account=first,key='charge')
        self.assertEqual(status, 200, intent)
        self.assertEqual(self.control('accounts/' + first + '/readiness',
            {'action':'disable_payouts','expected_provider_version':info['provider_version']})[0],409)
        self.assertEqual(self.control('resources/inspect', {'kind':'account','resource_id':first,'account':first},
                                      token=self.server.credentials.api_key)[0],403)

    def test_reset_cannot_retire_a_run_while_delivery_is_in_flight(self):
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                inner.rfile.read(int(inner.headers['Content-Length']))
                entered.set()
                release.wait(3)
                inner.send_response(200)
                inner.send_header('Content-Length','0')
                inner.end_headers()
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
            callback=f'http://127.0.0.1:{receiver.server_port}/event').start()
        self.account()
        event = self.control('events/list',{})[1]['events'][0]
        def deliver():
            try:
                results.append(self.control('events/deliver',{'delivery_id':event['delivery_id'],
                    'expected_state_version':self.store.run(self.run)['state_version']}))
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=deliver)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            version = self.store.run(self.run)['state_version']
            self.assertEqual(self.control('reset',{'expected_state_version':version})[0],409)
        finally:
            release.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(results[0][0],200)
        self.assertTrue(results[0][1]['delivery']['delivered'])
        self.assertEqual(self.control('reset',{'expected_state_version':version})[0],200)

    def test_admin_can_inspect_and_deliver_selected_event_out_of_order(self):
        received = []
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                payload = inner.rfile.read(int(inner.headers['Content-Length']))
                received.append(json.loads(payload)['id'])
                inner.send_response(200)
                inner.send_header('Content-Length','0')
                inner.end_headers()
            def log_message(inner, *args):
                pass
        receiver = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        thread = threading.Thread(target=receiver.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        self.server.close()
        self.server = SimulatorServer(self.store, clock=self.clock,
            callback=f'http://127.0.0.1:{receiver.server_port}/webhook').start()
        self.account('first')
        self.account('second')
        status, listing, _ = self.control('events/list', {})
        self.assertEqual(status, 200, listing)
        first, second = listing['events'][:2]
        self.assertNotEqual(first['event_id'], second['event_id'])
        status, result, _ = self.control('events/deliver',
            {'delivery_id':second['delivery_id'], 'expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status, 200, result)
        self.assertTrue(result['delivery']['delivered'])
        self.assertEqual(received, [second['event_id']])
        updated = self.control('events/list', {})[1]['events']
        self.assertEqual([row['delivery_state'] for row in updated[:2]], ['queued','delivered'])
        self.assertEqual([row['attempts'] for row in updated[:2]], [0,1])
        self.assertEqual(self.control('events/list', {}, token=self.server.credentials.api_key)[0],403)


    def test_billing_scopes_and_filters_never_widen_access(self):
        first = self.account('billing-first')
        second = self.account('billing-second')
        status, connected, _ = self.control('billing/subscriptions/seed', {
            'client_reference_id':'org_a', 'amount':'1200', 'account':first, 'expected_state_version':1})
        self.assertEqual(status,200,connected)
        status, platform, _ = self.control('billing/subscriptions/seed', {
            'client_reference_id':'org_b', 'amount':'1200', 'expected_state_version':connected['state_version']})
        self.assertEqual(status,200,platform)
        path = '/v1/subscriptions/' + connected['subscription']
        self.assertEqual(self.request('GET',path,account=first)[0],200)
        self.assertEqual(self.request('GET',path,account=second)[0],403)
        self.assertEqual(self.request('GET',path)[0],403)
        self.assertEqual(self.request('GET','/v1/subscriptions/' + platform['subscription'],account='')[0],403)
        self.assertEqual(self.request('GET','/v1/subscriptions?customer=all')[1]['data'],[])
        self.assertEqual(len(self.request('GET','/v1/subscriptions?status=all')[1]['data']),1)

    def test_terminal_intent_reads_do_not_exhaust_component_capabilities(self):
        status, seeded, _ = self.control('billing/subscriptions/seed', {
            'client_reference_id':'org_poll', 'amount':'1200', 'expected_state_version':1})
        self.assertEqual(status,200,seeded)
        for _ in range(40):
            status, intent, _ = self.request('GET','/v1/payment_intents/' + seeded['payment_intent'])
            self.assertEqual(status,200,intent)
            self.assertEqual(intent['status'],'succeeded')

    def test_platform_subscription_refunds_change_canonical_charge_totals(self):
        status, seeded, _ = self.control('billing/subscriptions/seed', {
            'client_reference_id':'org_refund', 'amount':'1200', 'expected_state_version':1})
        self.assertEqual(status,200,seeded)
        status, refund, _ = self.request('POST','/v1/refunds',
            {'charge':seeded['charge'],'amount':'400','refund_application_fee':'false'}, key='platform-refund')
        self.assertEqual(status,200,refund)
        self.assertEqual(refund['status'],'succeeded')
        status, charge, _ = self.request('GET','/v1/charges/' + seeded['charge'])
        self.assertEqual(status,200,charge)
        self.assertEqual(charge['amount_refunded'],400)
        listing = self.request('GET','/v1/charges?payment_intent=' + seeded['payment_intent'])[1]
        self.assertEqual(listing['data'][0]['amount_refunded'],400)
        self.assertEqual(self.request('POST','/v1/refunds',
            {'charge':seeded['charge'],'amount':'801','refund_application_fee':'false'}, key='too-much')[0],409)

    def test_subscription_fixture_accepts_generic_caller_metadata(self):
        status, seeded, _ = self.control('billing/subscriptions/seed', {
            'amount':'1200', 'client_reference_id':'workspace_42',
            'metadata':{'workspace':'workspace_42','plan':'annual'}, 'expected_state_version':1})
        self.assertEqual(status,200,seeded)
        subscription = self.request('GET','/v1/subscriptions/' + seeded['subscription'])[1]
        session = self.request('GET','/v1/checkout/sessions/' + seeded['session'])[1]
        self.assertEqual(subscription['metadata'],{'workspace':'workspace_42','plan':'annual'})
        self.assertEqual(session['client_reference_id'],'workspace_42')
        self.assertNotIn('organizationId',subscription['metadata'])
        status, plain, _ = self.control('billing/subscriptions/seed', {
            'amount':'1200', 'expected_state_version':seeded['state_version']})
        self.assertEqual(status,200,plain)
        self.assertEqual(self.request('GET','/v1/subscriptions/' + plain['subscription'])[1]['metadata'],{})
        self.assertIsNone(self.request('GET','/v1/checkout/sessions/' + plain['session'])[1]['client_reference_id'])
        for metadata in ({'bad key':'value'}, {'key':'not synthetic@example.com'}, {'key':1}):
            self.assertEqual(self.control('billing/subscriptions/seed', {
                'amount':'1200','metadata':metadata,'expected_state_version':plain['state_version']})[0],400)

    def test_subscription_fixture_has_consistent_read_paths(self):
        status, seeded, _ = self.control('billing/subscriptions/seed', {
            'client_reference_id':'org_example', 'amount':'1200',
            'expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status, 200, seeded)
        customer, price, subscription = seeded['customer'], seeded['price'], seeded['subscription']
        invoice, intent, charge = seeded['invoice'], seeded['payment_intent'], seeded['charge']
        for kind, identifier in [('customers',customer),('prices',price),('subscriptions',subscription),
                                 ('invoices',invoice),('checkout/sessions',seeded['session'])]:
            status, resource, _ = self.request('GET', '/v1/' + kind + '/' + identifier)
            self.assertEqual(status, 200, resource)
            self.assertEqual(resource['id'], identifier)
            self.assertFalse(resource['livemode'])
        status, payment_intent, _ = self.request('GET','/v1/payment_intents/' + intent)
        self.assertEqual(status,200,payment_intent)
        self.assertEqual(payment_intent['customer'],customer)
        self.assertEqual(self.request('GET','/v1/subscriptions/' + subscription)[1]['customer'],customer)
        self.assertEqual(self.request('GET','/v1/prices/' + price)[1]['recurring']['interval'],'year')
        invoices = self.request('GET','/v1/invoices?subscription=' + subscription + '&limit=100')[1]
        self.assertEqual([row['id'] for row in invoices['data']], [invoice])
        self.assertEqual(invoices['data'][0]['parent']['subscription_details']['subscription'],subscription)
        payments = self.request('GET','/v1/invoice_payments?invoice=' + invoice + '&status=paid&limit=100')[1]
        self.assertEqual(payments['data'][0]['payment']['payment_intent'],intent)
        charges = self.request('GET','/v1/charges?payment_intent=' + intent + '&limit=100')[1]
        self.assertEqual(charges['data'][0]['id'],charge)
        lines = self.request('GET','/v1/invoices/' + invoice + '/lines?limit=100')[1]
        self.assertEqual(lines['data'][0]['pricing']['price_details']['price'],price)
        events = self.control('events/list', {})[1]['events']
        self.assertEqual([event['type'] for event in events],
                         ['checkout.session.completed','customer.subscription.updated','invoice.paid'])
        self.assertEqual([event['resource_id'] for event in events], [seeded['session'],subscription,invoice])
        self.assertEqual(self.control('billing/subscriptions/seed', {
            'client_reference_id':'org_example', 'amount':'1200', 'expected_state_version':1})[0],409)
        self.assertEqual(len(self.control('events/list', {})[1]['events']),3)
        status, info, _ = self.control('resources/inspect', {'kind':'subscription','resource_id':subscription})
        self.assertEqual(status,200,info)
        status, changed, _ = self.control('billing/subscriptions/' + subscription + '/actions',
            {'action':'payment_failed','expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,changed)
        self.assertEqual(self.request('GET','/v1/subscriptions/' + subscription)[1]['status'],'past_due')
        self.assertEqual(self.control('billing/subscriptions/' + subscription + '/actions',
            {'action':'payment_failed','expected_provider_version':info['provider_version']})[0],409)
        self.assertEqual(self.control('billing/subscriptions/' + subscription + '/actions',
            {'action':'cancel','expected_provider_version':changed['provider_version']})[0],200)
        self.assertEqual(self.request('GET','/v1/subscriptions/' + subscription)[1]['status'],'canceled')
        self.assertEqual(self.control('events/list', {})[1]['events'][-1]['type'],'customer.subscription.deleted')


if __name__ == '__main__':
    unittest.main()
