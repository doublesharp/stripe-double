"""Explicit, version-checked subscription renewal and promotion scenarios."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch
import test_near_term


class RenewalTests(unittest.TestCase):
    setUp = test_near_term.NearTermHttpTests.setUp
    close = test_near_term.NearTermHttpTests.close
    request = test_near_term.NearTermHttpTests.request
    control = test_near_term.NearTermHttpTests.control

    def seed(self):
        self.clock.now = datetime(2026,1,31,10,tzinfo=timezone.utc).timestamp()
        status, seeded, _ = self.control('billing/subscriptions/seed',{'amount':'1200','interval':'month',
            'expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,200,seeded)
        self.clock.now = datetime(2026,2,28,10,tzinfo=timezone.utc).timestamp()
        return seeded

    def action(self, sub, action, version):
        return self.control('billing/subscriptions/' + sub + '/actions',
            {'action':action,'expected_provider_version':version})

    def info(self, sub):
        return self.control('resources/inspect',{'kind':'subscription','resource_id':sub})[1]

    def test_paid_renewal_creates_one_linked_invoice_and_keeps_calendar_anchor(self):
        seed = self.seed()
        info = self.info(seed['subscription'])
        status, result, _ = self.action(seed['subscription'],'renew',info['provider_version'])
        self.assertEqual(status,200,result)
        self.assertEqual(result['status'],'active')
        self.assertEqual(self.action(seed['subscription'],'renew',info['provider_version'])[0],409)
        sub = self.request('GET','/v1/subscriptions/' + seed['subscription'])[1]
        self.assertEqual(sub['current_period_end'],int(datetime(2026,3,31,10,tzinfo=timezone.utc).timestamp()))
        invoices = self.request('GET','/v1/invoices?subscription=' + sub['id'])[1]['data']
        self.assertEqual(len(invoices),2)
        renewal = next(i for i in invoices if i['id'] == result['invoice'])
        self.assertEqual((renewal['status'],renewal['amount_paid']),('paid',1200))
        payments = self.request('GET','/v1/invoice_payments?invoice=' + renewal['id'])[1]['data']
        self.assertEqual(len(payments),1)
        self.assertEqual(payments[0]['amount_paid'],1200)

    def test_failed_renewal_retries_existing_invoice_explicitly(self):
        seed = self.seed()
        info = self.info(seed['subscription'])
        status, failed, _ = self.action(seed['subscription'],'renew_fail',info['provider_version'])
        self.assertEqual(status,200,failed)
        self.assertEqual(failed['status'],'past_due')
        invoice = self.request('GET','/v1/invoices/' + failed['invoice'])[1]
        self.assertEqual((invoice['status'],invoice['amount_paid']),('open',0))
        self.assertEqual(self.action(seed['subscription'],'cancel',failed['provider_version'])[0],409)
        status, recovered, _ = self.action(seed['subscription'],'retry_renewal',failed['provider_version'])
        self.assertEqual(status,200,recovered)
        self.assertEqual((recovered['status'],recovered['invoice']),('active',invoice['id']))
        self.assertEqual(self.request('GET','/v1/invoices/' + invoice['id'])[1]['status'],'paid')
        self.assertEqual(len(self.request('GET','/v1/invoices?subscription=' + seed['subscription'])[1]['data']),2)

    def test_once_promotion_discounts_checkout_but_not_renewal(self):
        seed = self.seed()
        status, promotion, _ = self.control('billing/promotions/seed',{'code':'LOCAL_SAVE25','percent_off':'25',
            'duration':'once','expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,200,promotion)
        form = {'mode':'subscription','customer':seed['customer'],
                'line_items[0][price]':seed['price'],'line_items[0][quantity]':'1',
                'discounts[0][promotion_code]':promotion['promotion_code'],
                'success_url':'http://127.0.0.1/success','cancel_url':'http://127.0.0.1/cancel'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='promotion')
        self.assertEqual(status,200,session)
        info = self.control('resources/inspect',{'kind':'checkout_session','resource_id':session['id']})[1]
        status, completed, _ = self.control('checkout/sessions/' + session['id'] + '/actions',
            {'action':'complete','expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,completed)
        checkout = self.request('GET','/v1/checkout/sessions/' + session['id'])[1]
        self.assertEqual((checkout['amount_subtotal'],checkout['total_details']['amount_discount'],checkout['amount_total']),(1200,300,900))
        status, expanded, _ = self.request('GET','/v1/checkout/sessions/' + session['id'] + '?expand%5B%5D=discounts')
        self.assertEqual(status,200,expanded)
        self.assertEqual(expanded['discounts'][0]['promotion_code'],promotion['promotion_code'])
        sub = self.request('GET','/v1/subscriptions/' + completed['subscription'])[1]
        expanded_sub = self.request('GET','/v1/subscriptions/' + sub['id'] + '?expand%5B%5D=discounts')[1]
        self.assertEqual(expanded_sub['discounts'][0]['promotion_code'],promotion['promotion_code'])
        first_invoice = self.request('GET','/v1/invoices?subscription=' + sub['id'])[1]['data'][0]
        expanded_invoice = self.request('GET','/v1/invoices/' + first_invoice['id'] + '?expand%5B%5D=discounts')[1]
        self.assertEqual(expanded_invoice['discounts'][0]['promotion_code'],promotion['promotion_code'])
        self.assertEqual(expanded_invoice['total_discount_amounts'][0]['amount'],300)
        self.clock.now = sub['current_period_end']
        version = self.info(sub['id'])['provider_version']
        status, renewal, _ = self.action(sub['id'],'renew',version)
        self.assertEqual(status,200,renewal)
        self.assertEqual(self.request('GET','/v1/invoices/' + renewal['invoice'])[1]['amount_paid'],1200)
        updated = self.request('GET','/v1/subscriptions/' + sub['id'] + '?expand%5B%5D=discounts')[1]
        self.assertEqual(updated['discounts'],[])

    def test_forever_promotion_is_attributed_on_renewal_invoice(self):
        seed = self.seed()
        status, promo, _ = self.control('billing/promotions/seed',{'code':'LOCAL_FOREVER','percent_off':'25',
            'duration':'forever','expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,200,promo)
        form = {'mode':'subscription','customer':seed['customer'],
                'line_items[0][price]':seed['price'],'line_items[0][quantity]':'1',
                'discounts[0][promotion_code]':promo['promotion_code'],
                'success_url':'http://127.0.0.1/success','cancel_url':'http://127.0.0.1/cancel'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='forever')
        self.assertEqual(status,200,session)
        info = self.control('resources/inspect',{'kind':'checkout_session','resource_id':session['id']})[1]
        status, completed, _ = self.control('checkout/sessions/' + session['id'] + '/actions',
            {'action':'complete','expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,completed)
        sub = self.request('GET','/v1/subscriptions/' + completed['subscription'])[1]
        self.clock.now = sub['current_period_end']
        status, renewed, _ = self.action(sub['id'],'renew',self.info(sub['id'])['provider_version'])
        self.assertEqual(status,200,renewed)
        status, invoice, _ = self.request('GET','/v1/invoices/' + renewed['invoice'] + '?expand%5B%5D=discounts')
        self.assertEqual(status,200,invoice)
        self.assertEqual(invoice['amount_paid'],900)
        self.assertEqual(invoice['total_discount_amounts'][0]['amount'],300)
        self.assertEqual(invoice['discounts'][0]['promotion_code'],promo['promotion_code'])

    @patch('stripe_double.wire.MAX_EVENTS',10)
    def test_outbox_capacity_reserves_room_to_recover_failed_renewal(self):
        seed = self.seed()
        status, failed, _ = self.action(seed['subscription'],'renew_fail',self.info(seed['subscription'])['provider_version'])
        self.assertEqual(status,200,failed)
        status, _, _ = self.control('actions',{'action':'duplicate_last_event',
            'expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,429)
        status, recovered, _ = self.action(seed['subscription'],'retry_renewal',failed['provider_version'])
        self.assertEqual(status,200,recovered)
        self.assertEqual(recovered['status'],'active')
