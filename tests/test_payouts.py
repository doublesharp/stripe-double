"""Synthetic payout balances, reservations and lifecycle over HTTP."""
import json
import unittest
from unittest.mock import patch
import test_near_term


class PayoutTests(unittest.TestCase):
    setUp = test_near_term.NearTermHttpTests.setUp
    close = test_near_term.NearTermHttpTests.close
    request = test_near_term.NearTermHttpTests.request
    control = test_near_term.NearTermHttpTests.control
    account = test_near_term.NearTermHttpTests.account
    admin = test_near_term.NearTermHttpTests.admin
    ready = test_near_term.NearTermHttpTests.ready
    restart = test_near_term.test_http.HttpTests.restart

    def fund(self, account, amount):
        return self.control('balances/fund', {'account':account,'amount':str(amount),
            'expected_state_version':self.store.run(self.run)['state_version']})

    def test_payout_reservation_failure_and_account_isolation(self):
        first, second = self.account('first'), self.account('second')
        self.ready()
        self.assertEqual(self.fund(first,1000)[0],200)
        status, balance, _ = self.request('GET','/v1/balance',account=first)
        self.assertEqual(status,200,balance)
        self.assertEqual(balance['available'][0]['amount'],1000)
        status, payout, _ = self.request('POST','/v1/payouts',{'amount':'600','currency':'usd'},account=first,key='payout')
        self.assertEqual(status,200,payout)
        self.assertEqual(payout['status'],'pending')
        self.assertEqual(self.request('POST','/v1/payouts',{'amount':'600','currency':'usd'},account=first,key='payout')[1],payout)
        self.assertEqual(self.request('GET','/v1/balance',account=first)[1]['available'][0]['amount'],400)
        self.assertNotEqual(self.request('POST','/v1/payouts',{'amount':'401','currency':'usd'},account=first,key='excess')[0],200)
        self.assertNotEqual(self.request('GET','/v1/payouts/' + payout['id'],account=second)[0],200)
        info = self.control('resources/inspect',{'kind':'payout','resource_id':payout['id'],'account':first})[1]
        status, result, _ = self.control('payouts/' + payout['id'] + '/actions',
            {'action':'fail','account':first,'expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,result)
        self.assertEqual(self.request('GET','/v1/balance',account=first)[1]['available'][0]['amount'],1000)
        self.assertEqual(self.request('GET','/v1/payouts/' + payout['id'],account=first)[1]['status'],'failed')
        self.assertEqual(self.request('GET','/v1/balance',account=second)[1]['available'][0]['amount'],0)
        types = [json.loads(bytes(row['payload']))['type'] for row in self.store.events(self.run)]
        self.assertIn('payout.created',types)
        self.assertIn('payout.failed',types)

    def test_payout_paid_and_cancel_are_durable(self):
        account = self.account()
        self.ready()
        self.assertEqual(self.fund(account,1000)[0],200)
        payout = self.request('POST','/v1/payouts',{'amount':'300','currency':'usd'},account=account,key='paid')[1]
        info = self.control('resources/inspect',{'kind':'payout','resource_id':payout['id'],'account':account})[1]
        action = {'action':'pay','account':account,'expected_provider_version':info['provider_version']}
        self.assertEqual(self.control('payouts/' + payout['id'] + '/actions',action)[0],200)
        self.assertEqual(self.control('payouts/' + payout['id'] + '/actions',action)[0],409)
        pending = self.request('POST','/v1/payouts',{'amount':'400','currency':'usd'},account=account,key='cancel-me')[1]
        cancel_path = '/v1/payouts/' + pending['id'] + '/cancel'
        status, canceled, _ = self.request('POST',cancel_path,account=account,key='cancel')
        self.assertEqual(status,200,canceled)
        self.assertEqual(canceled['status'],'canceled')
        self.restart()
        self.assertEqual(self.request('GET','/v1/balance',account=account)[1]['available'][0]['amount'],700)
        self.assertEqual(self.request('GET','/v1/payouts/' + payout['id'],account=account)[1]['status'],'paid')
        self.assertEqual(self.request('POST',cancel_path,account=account,key='cancel')[1],canceled)
        page = self.request('GET','/v1/payouts?limit=10',account=account)[1]
        self.assertEqual({p['id'] for p in page['data']},{payout['id'],pending['id']})

    @patch('stripe_double.state.MAX_BALANCE',99999999)
    def test_pending_reservations_preserve_capacity_to_release(self):
        account = self.account()
        self.ready()
        self.assertEqual(self.fund(account,99999999)[0],200)
        status, payout, _ = self.request('POST','/v1/payouts',{'amount':'100','currency':'usd'},account=account,key='reserve')
        self.assertEqual(status,200,payout)
        self.assertEqual(self.fund(account,1)[0],429)
        self.assertEqual(self.request('POST','/v1/payouts/' + payout['id'] + '/cancel',account=account,key='return')[0],200)
        self.assertEqual(self.request('GET','/v1/balance',account=account)[1]['available'][0]['amount'],99999999)

    @patch('stripe_double.wire.MAX_EVENTS',4)
    def test_outbox_capacity_cannot_strand_a_pending_payout(self):
        account = self.account()
        self.ready()
        self.assertEqual(self.fund(account,1000)[0],200)
        status, payout, _ = self.request('POST','/v1/payouts',{'amount':'400','currency':'usd'},account=account,key='reserved')
        self.assertEqual(status,200,payout)
        self.assertEqual(self.admin('duplicate_last_event')[0],429)
        status, canceled, _ = self.request('POST','/v1/payouts/'+payout['id']+'/cancel',account=account,key='cancel')
        self.assertEqual(status,200,canceled)
        self.assertEqual(canceled['status'],'canceled')
        self.assertEqual(self.request('GET','/v1/balance',account=account)[1]['available'][0]['amount'],1000)
