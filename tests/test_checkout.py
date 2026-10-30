"""Observable synthetic Checkout and portal initiation contracts."""
import json
import unittest
from unittest.mock import patch

import test_near_term
from stripe_double.wire import SimulatorError


class CheckoutTests(unittest.TestCase):
    setUp = test_near_term.NearTermHttpTests.setUp
    close = test_near_term.NearTermHttpTests.close
    request = test_near_term.NearTermHttpTests.request
    control = test_near_term.NearTermHttpTests.control
    account = test_near_term.NearTermHttpTests.account
    restart = test_near_term.test_http.HttpTests.restart
    def seed(self):
        status, seeded, _ = self.control('billing/subscriptions/seed', {
            'amount':'1200', 'expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status, 200, seeded)
        return seeded

    def test_hosted_checkout_completion_and_canonical_reads(self):
        seed = self.seed()
        form = {'mode':'subscription','customer':seed['customer'],
                'line_items[0][price]':seed['price'],'line_items[0][quantity]':'1',
                'success_url':'http://127.0.0.1:3000/success',
                'cancel_url':'http://127.0.0.1:3000/cancel',
                'client_reference_id':'org_example',
                'subscription_data[metadata][organizationId]':'org_example'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='checkout')
        self.assertEqual(status, 200, session)
        self.assertEqual((session['status'], session['payment_status']), ('open', 'unpaid'))
        self.assertTrue(session['url'])
        self.assertEqual(self.request('POST','/v1/checkout/sessions',form,key='checkout')[1],session)
        status, info, _ = self.control('resources/inspect', {'kind':'checkout_session','resource_id':session['id']})
        self.assertEqual(status, 200, info)
        action = {'action':'complete','expected_provider_version':info['provider_version']}
        status, completed, _ = self.control('checkout/sessions/' + session['id'] + '/actions',action)
        self.assertEqual(status, 200, completed)
        status, observed, _ = self.request('GET','/v1/checkout/sessions/' + session['id'])
        self.assertEqual(status, 200, observed)
        self.assertEqual((observed['status'],observed['payment_status']),('complete','paid'))
        self.assertEqual(observed['customer'],seed['customer'])
        status, subscription, _ = self.request('GET','/v1/subscriptions/' + observed['subscription'])
        self.assertEqual(status,200,subscription)
        self.assertEqual(subscription['customer'],seed['customer'])
        self.assertEqual(subscription['metadata']['organizationId'],'org_example')
        events = [json.loads(bytes(row['payload'])) for row in self.store.events(self.run)]
        event = next(e for e in events if e['type'] == 'checkout.session.completed' and e['data']['object']['id'] == session['id'])
        self.assertEqual(event['data']['object']['subscription'],observed['subscription'])
        self.assertEqual(self.control('checkout/sessions/' + session['id'] + '/actions',action)[0],409)

    def test_elements_and_portal_are_scoped_and_reject_unmodeled_tax(self):
        seed = self.seed()
        form = {'mode':'subscription','customer':seed['customer'],
                'line_items[0][price]':seed['price'],'line_items[0][quantity]':'1',
                'ui_mode':'elements','return_url':'http://127.0.0.1:3000/return'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='elements')
        self.assertEqual(status,200,session)
        self.assertTrue(session['client_secret'])
        self.assertIsNone(session['url'])
        status, _, _ = self.request('POST','/v1/checkout/sessions',
            {**form,'automatic_tax[enabled]':'true'},key='tax')
        self.assertEqual(status,400)
        status, portal, _ = self.request('POST','/v1/billing_portal/sessions',
            {'customer':seed['customer'],'return_url':'http://127.0.0.1:3000/'},key='portal')
        self.assertEqual(status,200,portal)
        self.assertTrue(portal['url'])
        other = self.account()
        self.assertNotEqual(self.request('POST','/v1/billing_portal/sessions',
            {'customer':seed['customer'],'return_url':'http://127.0.0.1:3000/'},account=other,key='cross')[0],200)

    def test_restart_expiry_idempotency_and_secrets(self):
        seed = self.seed()
        form = {'mode':'subscription', 'customer':seed['customer'],
                'line_items[0][price]':seed['price'], 'line_items[0][quantity]':'1',
                'ui_mode':'elements', 'return_url':'http://127.0.0.1/return'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='elements')
        self.assertEqual(status,200,session)
        secret = session['client_secret']
        self.assertNotIn(secret, self.path.read_bytes().decode('latin1'))
        self.assertEqual(self.request('POST','/v1/checkout/sessions',
            {**form,'client_reference_id':'changed'},key='elements')[0],409)
        self.restart()
        status, observed, _ = self.request('GET','/v1/checkout/sessions/' + session['id'])
        self.assertEqual(status,200,observed)
        self.assertEqual(observed['status'],'open')
        self.assertNotIn('client_secret',observed)
        info = self.control('resources/inspect', {'kind':'checkout_session','resource_id':session['id']})[1]
        self.clock.advance(86401)
        action = {'action':'complete','expected_provider_version':info['provider_version']}
        self.assertEqual(self.control('checkout/sessions/' + session['id'] + '/actions',action)[0],409)
        action['action'] = 'expire'
        self.assertEqual(self.control('checkout/sessions/' + session['id'] + '/actions',action)[0],200)
        self.assertEqual(self.request('GET','/v1/checkout/sessions/' + session['id'])[1]['status'],'expired')

    def test_customer_and_checkout_list_scope(self):
        seed = self.seed()
        status, customer, _ = self.request('POST','/v1/customers',{'email':'test@example.test'},key='customer')
        self.assertEqual(status,200,customer)
        self.assertEqual(self.request('POST','/v1/customers',{'email':'real@example.com'},key='real')[0],400)
        form = {'mode':'subscription','customer':customer['id'],
                'line_items[0][price]':seed['price'],'line_items[0][quantity]':'1',
                'success_url':'http://127.0.0.1/success','cancel_url':'http://127.0.0.1/cancel'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='new')
        self.assertEqual(status,200,session)
        status, page, _ = self.request('GET','/v1/checkout/sessions?customer=' + customer['id'])
        self.assertEqual(status,200,page)
        self.assertEqual([x['id'] for x in page['data']],[session['id']])
        self.assertEqual(self.request('POST','/v1/checkout/sessions',
            {**form,'success_url':'http://[invalid'},key='invalid')[0],400)

    def test_customerless_checkout_creates_one_scoped_synthetic_customer(self):
        seed = self.seed()
        form = {'mode':'subscription', 'customer_email':'buyer@example.test',
                'line_items[0][price]':seed['price'], 'line_items[0][quantity]':'1',
                'success_url':'http://127.0.0.1/success', 'cancel_url':'http://127.0.0.1/cancel'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='customerless')
        self.assertEqual(status,200,session)
        self.assertIsNone(session['customer'])
        info = self.control('resources/inspect', {'kind':'checkout_session','resource_id':session['id']})[1]
        action = {'action':'complete','expected_provider_version':info['provider_version']}
        self.assertEqual(self.control('checkout/sessions/' + session['id'] + '/actions',action)[0],200)
        completed = self.request('GET','/v1/checkout/sessions/' + session['id'])[1]
        customer = self.request('GET','/v1/customers/' + completed['customer'])[1]
        self.assertEqual(customer['email'],'buyer@example.test')
        self.assertEqual(self.control('checkout/sessions/' + session['id'] + '/actions',action)[0],409)
        self.assertEqual(len(self.store.objects(self.run,'customer')),2)

    def test_completion_rolls_back_when_outbox_write_fails(self):
        seed = self.seed()
        form = {'mode':'subscription','customer':seed['customer'],
                'line_items[0][price]':seed['price'],'line_items[0][quantity]':'1',
                'success_url':'http://127.0.0.1/success','cancel_url':'http://127.0.0.1/cancel'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='atomic')
        self.assertEqual(status,200,session)
        info = self.control('resources/inspect', {'kind':'checkout_session','resource_id':session['id']})[1]
        action = {'action':'complete','expected_provider_version':info['provider_version']}
        path = 'checkout/sessions/' + session['id'] + '/actions'
        with patch.object(self.store,'_event',side_effect=SimulatorError(429,'simulator_limit')):
            self.assertEqual(self.control(path,action)[0],429)
        self.assertEqual(self.request('GET','/v1/checkout/sessions/' + session['id'])[1]['status'],'open')
        self.assertEqual(len(self.store.objects(self.run,'subscription')),1)
        self.assertEqual(self.control(path,action)[0],200)
