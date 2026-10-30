"""Synthetic charge, settlement, payout, refund and fee conservation."""
import unittest
from unittest.mock import patch
import test_near_term
from test_state import FIXTURE


class BalanceFlowTests(unittest.TestCase):
    setUp = test_near_term.NearTermHttpTests.setUp
    close = test_near_term.NearTermHttpTests.close
    request = test_near_term.NearTermHttpTests.request
    control = test_near_term.NearTermHttpTests.control
    account = test_near_term.NearTermHttpTests.account
    admin = test_near_term.NearTermHttpTests.admin
    ready = test_near_term.NearTermHttpTests.ready

    def state_version(self):
        return self.store.run(self.run)['state_version']

    def settle(self, account=None):
        body = {'expected_state_version':self.state_version()}
        if account is not None:
            body['account'] = account
        return self.control('balances/settle',body)

    def balance(self, account=None):
        status, result, _ = self.request('GET','/v1/balance',account=account)
        self.assertEqual(status,200,result)
        return result['available'][0]['amount'],result['pending'][0]['amount']

    def test_capture_settle_payout_refund_and_fee_refund_conserve_balances(self):
        account = self.account()
        self.ready()
        status, _, _ = self.control('balances/configure',{'account':account,'fee_bps':'300','fee_flat':'30',
            'settlement_delay':'3600','expected_state_version':self.state_version()})
        self.assertEqual(status,200)
        intent_form = dict(FIXTURE['intent_form'])
        status, intent, _ = self.request('POST','/v1/payment_intents',intent_form,account=account,key='intent')
        self.assertEqual(status,200,intent)
        status, _, _ = self.request('POST','/v1/payment_intents/'+intent['id']+'/confirm',
            {'payment_method':'pm_card_visa'},account=account,key='confirm')
        self.assertEqual(status,200)
        capture_path = '/v1/payment_intents/'+intent['id']+'/capture'
        status, captured, _ = self.request('POST',capture_path,FIXTURE['capture_form'],account=account,key='capture')
        self.assertEqual(status,200,captured)
        charge = captured['latest_charge']
        expanded = self.request('GET','/v1/charges/'+charge['id']+'?expand%5B%5D=balance_transaction',account=account)[1]
        self.assertIsInstance(expanded['balance_transaction'],dict)
        self.assertEqual(expanded['balance_transaction']['net'],840)
        self.assertEqual(self.request('POST',capture_path,FIXTURE['capture_form'],account=account,key='capture')[1],captured)
        self.assertEqual(self.balance(account),(0,840))
        self.assertEqual(self.balance(),(0,100))
        status, txn, _ = self.request('GET','/v1/balance_transactions/'+charge['balance_transaction'],account=account)
        self.assertEqual(status,200,txn)
        self.assertEqual((txn['amount'],txn['fee'],txn['net'],txn['status']),(1000,160,840,'pending'))
        self.assertEqual(self.settle(account)[1]['settled'],0)
        self.clock.advance(3600)
        self.assertEqual(self.settle(account)[1]['settled'],1)
        self.assertEqual(self.settle()[1]['settled'],1)
        self.assertEqual(self.balance(account),(840,0))
        self.assertEqual(self.balance(),(100,0))
        status, payout, _ = self.request('POST','/v1/payouts',{'amount':'500','currency':'usd'},account=account,key='payout')
        self.assertEqual(status,200,payout)
        status, refund, _ = self.request('POST','/v1/refunds',{'charge':charge['id'],'amount':'600',
            'refund_application_fee':'false'},account=account,key='refund')
        self.assertEqual(status,200,refund)
        expanded_refund = self.request('GET','/v1/refunds/'+refund['id']+'?expand%5B%5D=balance_transaction',account=account)[1]
        self.assertIsInstance(expanded_refund['balance_transaction'],dict)
        self.assertEqual(expanded_refund['balance_transaction']['net'],-600)
        self.assertEqual(self.balance(account),(-260,0))
        status, _, _ = self.request('POST','/v1/payouts/'+payout['id']+'/cancel',account=account,key='cancel')
        self.assertEqual(status,200)
        self.assertEqual(self.balance(account),(240,0))
        status, _, _ = self.request('POST','/v1/application_fees/'+charge['application_fee']+'/refunds',
            {'amount':'50'},key='fee-refund')
        self.assertEqual(status,200)
        self.assertEqual(self.balance(account),(290,0))
        self.assertEqual(self.balance(),(50,0))

    def test_pending_refund_does_not_debit_until_success(self):
        account = self.account()
        self.ready()
        self.assertEqual(self.admin('refund_pending')[0],200)
        status, intent, _ = self.request('POST','/v1/payment_intents',FIXTURE['intent_form'],account=account,key='intent')
        self.assertEqual(status,200,intent)
        self.assertEqual(self.request('POST','/v1/payment_intents/'+intent['id']+'/confirm',
            {'payment_method':'pm_card_visa'},account=account,key='confirm')[0],200)
        charge = self.request('POST','/v1/payment_intents/'+intent['id']+'/capture',FIXTURE['capture_form'],
            account=account,key='capture')[1]['latest_charge']
        before = self.balance(account)
        status, refund, _ = self.request('POST','/v1/refunds',{'charge':charge['id'],'amount':'300',
            'refund_application_fee':'false'},account=account,key='pending')
        self.assertEqual(status,200,refund)
        self.assertEqual(self.balance(account),before)
        info = self.control('resources/inspect',{'kind':'refund','resource_id':refund['id'],'account':account})[1]
        status, _, _ = self.control('refunds/'+refund['id']+'/actions',
            {'action':'succeed','account':account,'expected_provider_version':info['provider_version']})
        self.assertEqual(status,200)
        self.assertEqual(self.balance(account),(before[0]-300,before[1]))

    def test_card_and_bank_sources_cannot_fund_each_others_payouts(self):
        card = self.request('POST','/v1/payment_intents',{'amount':'1000','currency':'usd'},key='card')[1]
        self.assertEqual(self.request('POST','/v1/payment_intents/'+card['id']+'/confirm',
            {'payment_method':'pm_card_visa'},key='card-confirm')[0],200)
        bank = self.request('POST','/v1/payment_intents',{'amount':'1200','currency':'usd',
            'allowed_payment_method_types[]':'us_bank_account'},key='bank')[1]
        self.assertEqual(self.request('POST','/v1/payment_intents/'+bank['id']+'/confirm',
            {'payment_method':'pm_usBankAccount_processing'},key='bank-confirm')[0],200)
        info = self.control('resources/inspect',{'kind':'payment_intent','resource_id':bank['id']})[1]
        self.assertEqual(self.control('payment_intents/'+bank['id']+'/bank_actions',
            {'action':'succeed','expected_provider_version':info['provider_version']})[0],200)
        self.clock.advance(172800)
        self.assertEqual(self.settle()[0],200)
        balance = self.request('GET','/v1/balance')[1]['available'][0]
        self.assertEqual(balance['source_types']['card'],1000)
        self.assertEqual(balance['source_types']['bank_account'],1200)
        self.assertNotEqual(self.request('POST','/v1/payouts',{'amount':'1100','currency':'usd',
            'source_type':'card'},key='card-excess')[0],200)
        self.assertEqual(self.request('POST','/v1/payouts',{'amount':'1100','currency':'usd',
            'source_type':'bank_account'},key='bank-payout')[0],200)
        balance = self.request('GET','/v1/balance')[1]['available'][0]
        self.assertEqual((balance['source_types']['card'],balance['source_types']['bank_account']),(1000,100))

    def test_dispute_reservation_and_win_prevent_double_refund(self):
        intent = self.request('POST','/v1/payment_intents',{'amount':'1000','currency':'usd'},key='dispute')[1]
        paid = self.request('POST','/v1/payment_intents/'+intent['id']+'/confirm',
            {'payment_method':'pm_card_visa'},key='dispute-confirm')[1]
        charge = paid['latest_charge']['id']
        info = self.control('resources/inspect',{'kind':'charge','resource_id':charge})[1]
        status, opened, _ = self.control('charges/'+charge+'/disputes',{'expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,opened)
        self.assertEqual(self.balance(),(-1000,1000))
        refund_form = {'charge':charge,'amount':'300','refund_application_fee':'false'}
        self.assertEqual(self.request('POST','/v1/refunds',refund_form,key='during-dispute')[0],409)
        info = self.control('resources/inspect',{'kind':'dispute','resource_id':opened['dispute']})[1]
        self.assertEqual(self.control('disputes/'+opened['dispute']+'/actions',
            {'action':'win','expected_provider_version':info['provider_version']})[0],200)
        self.assertEqual(self.balance(),(0,1000))
        self.assertEqual(self.request('POST','/v1/refunds',refund_form,key='after-dispute')[0],200)
        self.assertEqual(self.balance(),(-300,1000))

    @patch('stripe_double.state.MAX_BALANCE',99999999)
    def test_source_capacity_reserves_room_to_cancel_payout(self):
        bank = self.request('POST','/v1/payment_intents',{'amount':'99999900','currency':'usd',
            'allowed_payment_method_types[]':'us_bank_account'},key='large-bank')[1]
        self.assertEqual(self.request('POST','/v1/payment_intents/'+bank['id']+'/confirm',
            {'payment_method':'pm_usBankAccount_processing'},key='large-confirm')[0],200)
        info = self.control('resources/inspect',{'kind':'payment_intent','resource_id':bank['id']})[1]
        self.assertEqual(self.control('payment_intents/'+bank['id']+'/bank_actions',
            {'action':'succeed','expected_provider_version':info['provider_version']})[0],200)
        charge = self.request('GET','/v1/payment_intents/'+bank['id'])[1]['latest_charge']['id']
        self.clock.advance(172800)
        self.assertEqual(self.settle()[0],200)
        paid = self.request('POST','/v1/payouts',{'amount':'99999900','currency':'usd',
            'source_type':'bank_account'},key='paid-bank')[1]
        info = self.control('resources/inspect',{'kind':'payout','resource_id':paid['id']})[1]
        self.assertEqual(self.control('payouts/'+paid['id']+'/actions',
            {'action':'pay','expected_provider_version':info['provider_version']})[0],200)
        self.assertEqual(self.control('balances/fund',{'amount':'99999900','expected_state_version':self.state_version()})[0],200)
        reserved = self.request('POST','/v1/payouts',{'amount':'99999900','currency':'usd'},key='reserved-card')[1]
        self.assertEqual(self.request('POST','/v1/refunds',{'charge':charge,'amount':'99999900',
            'refund_application_fee':'false'},key='bank-refund')[0],200)
        card = self.request('POST','/v1/payment_intents',{'amount':'1000','currency':'usd'},key='extra-card')[1]
        self.assertEqual(self.request('POST','/v1/payment_intents/'+card['id']+'/confirm',
            {'payment_method':'pm_card_visa'},key='extra-confirm')[0],429)
        self.assertEqual(self.request('POST','/v1/payouts/'+reserved['id']+'/cancel',key='release-card')[0],200)

    def test_accounting_capacity_failure_rolls_back_payment_and_idempotency(self):
        intent = self.request('POST','/v1/payment_intents',{'amount':'1000','currency':'usd'},key='bounded')[1]
        path = '/v1/payment_intents/'+intent['id']+'/confirm'
        form = {'payment_method':'pm_card_visa'}
        with patch('stripe_double.state.MAX_ACCOUNTING_OBJECTS',0):
            self.assertEqual(self.request('POST',path,form,key='bounded-confirm')[0],429)
        with patch('stripe_double.state.MAX_ACCOUNTING_OBJECTS',1):
            self.assertEqual(self.request('POST',path,form,key='bounded-confirm')[0],429)
        self.assertEqual(self.request('GET','/v1/payment_intents/'+intent['id'])[1]['status'],'requires_payment_method')
        self.assertEqual(self.request('POST',path,form,key='bounded-confirm')[0],200)

    @patch('stripe_double.wire.MAX_EVENTS',4)
    def test_outbox_capacity_reserves_room_to_resolve_dispute(self):
        intent = self.request('POST','/v1/payment_intents',{'amount':'1000','currency':'usd'},key='event-limit')[1]
        paid = self.request('POST','/v1/payment_intents/'+intent['id']+'/confirm',
            {'payment_method':'pm_card_visa'},key='event-confirm')[1]
        charge = paid['latest_charge']['id']
        info = self.control('resources/inspect',{'kind':'charge','resource_id':charge})[1]
        status, dispute, _ = self.control('charges/'+charge+'/disputes',{'expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,dispute)
        self.assertEqual(self.admin('duplicate_last_event')[0],429)
        info = self.control('resources/inspect',{'kind':'dispute','resource_id':dispute['dispute']})[1]
        self.assertEqual(self.control('disputes/'+dispute['dispute']+'/actions',
            {'action':'win','expected_provider_version':info['provider_version']})[0],200)

    @patch('stripe_double.state.MAX_ACCOUNTING_OBJECTS',4)
    def test_accounting_capacity_reserves_room_to_release_disputed_funds(self):
        intent = self.request('POST','/v1/payment_intents',{'amount':'1000','currency':'usd'},key='reserve-dispute')[1]
        paid = self.request('POST','/v1/payment_intents/'+intent['id']+'/confirm',
            {'payment_method':'pm_card_visa'},key='reserve-dispute-confirm')[1]
        charge = paid['latest_charge']['id']
        info = self.control('resources/inspect',{'kind':'charge','resource_id':charge})[1]
        status, dispute, _ = self.control('charges/'+charge+'/disputes',{'expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,dispute)
        other = self.request('POST','/v1/payment_intents',{'amount':'1000','currency':'usd'},key='other')[1]
        self.assertEqual(self.request('POST','/v1/payment_intents/'+other['id']+'/confirm',
            {'payment_method':'pm_card_visa'},key='other-confirm')[0],429)
        info = self.control('resources/inspect',{'kind':'dispute','resource_id':dispute['dispute']})[1]
        self.assertEqual(self.control('disputes/'+dispute['dispute']+'/actions',
            {'action':'win','expected_provider_version':info['provider_version']})[0],200)
