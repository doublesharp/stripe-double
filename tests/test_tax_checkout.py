"""Opt-in synthetic tax fixtures for Checkout request options."""
import unittest
import test_checkout


class TaxCheckoutTests(unittest.TestCase):
    setUp = test_checkout.CheckoutTests.setUp
    close = test_checkout.CheckoutTests.close
    request = test_checkout.CheckoutTests.request
    control = test_checkout.CheckoutTests.control
    seed = test_checkout.CheckoutTests.seed

    def test_explicit_synthetic_tax_configuration_and_checkout_totals(self):
        seed = self.seed()
        status, settings, _ = self.request('GET','/v1/tax/settings')
        self.assertEqual(status,200,settings)
        self.assertNotEqual(settings['status'],'active')
        status, _, _ = self.control('tax/settings/configure',{'rate_bps':'1000',
            'expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,200)
        self.assertEqual(self.request('GET','/v1/tax/settings')[1]['status'],'active')
        form = {'mode':'subscription','customer':seed['customer'],
                'line_items[0][price]':seed['price'],'line_items[0][quantity]':'1',
                'success_url':'http://127.0.0.1/success','cancel_url':'http://127.0.0.1/cancel',
                'automatic_tax[enabled]':'true','tax_id_collection[enabled]':'true',
                'billing_address_collection':'required','customer_update[address]':'auto','customer_update[name]':'auto'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='taxed')
        self.assertEqual(status,200,session)
        status, _, _ = self.control('tax/settings/configure',{'rate_bps':'2000',
            'expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,200)
        info = self.control('resources/inspect',{'kind':'checkout_session','resource_id':session['id']})[1]
        status, result, _ = self.control('checkout/sessions/' + session['id'] + '/actions',
            {'action':'complete','expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,result)
        completed = self.request('GET','/v1/checkout/sessions/' + session['id'])[1]
        self.assertEqual(completed['automatic_tax']['status'],'complete')
        self.assertEqual(completed['amount_subtotal'],1200)
        self.assertEqual(completed['total_details']['amount_tax'],120)
        self.assertEqual(completed['amount_total'],1320)
        invoices = self.request('GET','/v1/invoices?subscription=' + result['subscription'])[1]
        self.assertEqual(invoices['data'][0]['amount_paid'],1320)
        charges = self.store.objects(self.run,'charge')
        self.assertEqual(sorted(charge['amount'] for charge in charges),[1200,1320])
