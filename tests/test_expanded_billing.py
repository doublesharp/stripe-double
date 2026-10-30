"""Portal configuration and hosted fee request contracts."""
import unittest
from datetime import datetime, timezone
import test_checkout
import test_near_term


class ExpandedBillingTests(unittest.TestCase):
    setUp = test_checkout.CheckoutTests.setUp
    close = test_checkout.CheckoutTests.close
    request = test_checkout.CheckoutTests.request
    control = test_checkout.CheckoutTests.control
    account = test_checkout.CheckoutTests.account
    admin = test_near_term.NearTermHttpTests.admin
    ready = test_near_term.NearTermHttpTests.ready
    seed = test_checkout.CheckoutTests.seed

    def configuration(self):
        return {'features[customer_update][enabled]':'false',
                'features[invoice_history][enabled]':'true',
                'features[payment_method_update][enabled]':'true',
                'features[subscription_cancel][enabled]':'true',
                'features[subscription_cancel][mode]':'at_period_end',
                'features[subscription_cancel][proration_behavior]':'none',
                'features[subscription_update][enabled]':'false',
                'login_page[enabled]':'false',
                'default_return_url':'http://127.0.0.1:3000/seller',
                'metadata[local_policy]':'period_end_v1'}

    def test_configuration_round_trip_and_portal_session(self):
        seeded = self.seed()
        status, before, _ = self.request('GET','/v1/billing_portal/configurations?active=true&limit=100')
        self.assertEqual(status,200,before)
        self.assertEqual(before['data'],[])
        form = self.configuration()
        status, config, _ = self.request('POST','/v1/billing_portal/configurations',form,key='configuration')
        self.assertEqual(status,200,config)
        self.assertEqual(config['metadata']['local_policy'],'period_end_v1')
        self.assertEqual(config['features']['subscription_cancel']['mode'],'at_period_end')
        self.assertFalse(config['features']['subscription_update']['enabled'])
        self.assertEqual(self.request('POST','/v1/billing_portal/configurations',form,key='configuration')[1],config)
        self.assertEqual(self.request('GET','/v1/billing_portal/configurations/' + config['id'])[1],config)
        status, listing, _ = self.request('GET','/v1/billing_portal/configurations?active=true&limit=100')
        self.assertEqual(status,200,listing)
        self.assertEqual([c['id'] for c in listing['data']],[config['id']])
        status, portal, _ = self.request('POST','/v1/billing_portal/sessions',{
            'customer':seeded['customer'],'configuration':config['id'],
            'return_url':'http://127.0.0.1:3000/seller'},key='portal')
        self.assertEqual(status,200,portal)
        self.assertEqual(portal['configuration'],config['id'])
        other = self.account()
        self.assertNotEqual(self.request('GET','/v1/billing_portal/configurations/' + config['id'],account=other)[0],200)

    def test_connected_checkout_accepts_bounded_fee_percent(self):
        account = self.account()
        self.ready()
        status, seeded, _ = self.control('billing/subscriptions/seed',{
            'amount':'1200','account':account,'expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,200,seeded)
        form = {'mode':'subscription','customer':seeded['customer'],
                'line_items[0][price]':seeded['price'],'line_items[0][quantity]':'1',
                'subscription_data[application_fee_percent]':'5',
                'success_url':'http://127.0.0.1/success','cancel_url':'http://127.0.0.1/cancel'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,account=account,key='fee')
        self.assertEqual(status,200,session)
        info = self.control('resources/inspect',{'kind':'checkout_session','resource_id':session['id'],'account':account})[1]
        status, completed, _ = self.control('checkout/sessions/' + session['id'] + '/actions',
            {'action':'complete','expected_provider_version':info['provider_version'],'account':account})
        self.assertEqual(status,200,completed)
        status, sub, _ = self.request('GET','/v1/subscriptions/' + completed['subscription'],account=account)
        self.assertEqual(status,200,sub)
        self.assertEqual(sub['application_fee_percent'],5)
        invoice = self.request('GET','/v1/invoices?subscription=' + sub['id'],account=account)[1]['data'][0]
        payment = self.request('GET','/v1/invoice_payments?invoice=' + invoice['id'],account=account)[1]['data'][0]
        charges = self.request('GET','/v1/charges?payment_intent=' + payment['payment']['payment_intent'],account=account)[1]['data']
        self.assertEqual(charges[0]['application_fee_amount'],60)
        self.assertEqual(self.request('GET','/v1/application_fees/' + charges[0]['application_fee'])[1]['amount'],60)

    def test_monthly_price_supports_platform_checkout(self):
        self.clock.now = datetime(2026,1,31,10,tzinfo=timezone.utc).timestamp()
        status, seeded, _ = self.control('billing/subscriptions/seed',{
            'amount':'1800','interval':'month','expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,200,seeded)
        status, price, _ = self.request('GET','/v1/prices/' + seeded['price'])
        self.assertEqual(status,200,price)
        self.assertEqual(price['recurring']['interval'],'month')
        status, customer, _ = self.request('POST','/v1/customers',{'email':'owner@example.test'},key='owner')
        self.assertEqual(status,200,customer)
        form = {'mode':'subscription','customer':customer['id'],
                'line_items[0][price]':seeded['price'],'line_items[0][quantity]':'1',
                'success_url':'http://127.0.0.1/success','cancel_url':'http://127.0.0.1/cancel'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='monthly')
        self.assertEqual(status,200,session)
        info = self.control('resources/inspect',{'kind':'checkout_session','resource_id':session['id']})[1]
        status, result, _ = self.control('checkout/sessions/' + session['id'] + '/actions',
            {'action':'complete','expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,result)
        sub = self.request('GET','/v1/subscriptions/' + result['subscription'])[1]
        self.assertEqual(sub['items']['data'][0]['current_period_end'],
                         int(datetime(2026,2,28,10,tzinfo=timezone.utc).timestamp()))

    def test_checkout_refuses_completion_when_connected_charges_are_disabled(self):
        account = self.account()
        self.ready()
        seeded = self.control('billing/subscriptions/seed',{'amount':'1200','account':account,
            'expected_state_version':self.store.run(self.run)['state_version']})[1]
        form = {'mode':'subscription','customer':seeded['customer'],
                'line_items[0][price]':seeded['price'],'line_items[0][quantity]':'1',
                'subscription_data[application_fee_percent]':'5',
                'success_url':'http://127.0.0.1/success','cancel_url':'http://127.0.0.1/cancel'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,account=account,key='restricted')
        self.assertEqual(status,200,session)
        account_info = self.control('resources/inspect',{'kind':'account','resource_id':account,'account':account})[1]
        status, _, _ = self.control('accounts/' + account + '/readiness',
            {'action':'disable_charges','expected_provider_version':account_info['provider_version']})
        self.assertEqual(status,200)
        session_info = self.control('resources/inspect',{'kind':'checkout_session','resource_id':session['id'],'account':account})[1]
        event_count = len(self.store.events(self.run))
        status, _, _ = self.control('checkout/sessions/' + session['id'] + '/actions',
            {'action':'complete','account':account,'expected_provider_version':session_info['provider_version']})
        self.assertEqual(status,409)
        self.assertEqual(self.request('GET','/v1/checkout/sessions/' + session['id'],account=account)[1]['status'],'open')
        self.assertEqual(len(self.store.objects(self.run,'subscription')),1)
        self.assertEqual(len(self.store.events(self.run)),event_count)
