"""Finite synthetic ACH processing using no bank or consent data."""
import json
import unittest
from unittest.mock import patch
import test_near_term


class BankPaymentTests(unittest.TestCase):
    setUp = test_near_term.NearTermHttpTests.setUp
    close = test_near_term.NearTermHttpTests.close
    request = test_near_term.NearTermHttpTests.request
    control = test_near_term.NearTermHttpTests.control

    def create(self, key):
        status, intent, _ = self.request('POST','/v1/payment_intents',{
            'amount':'1200','currency':'usd','allowed_payment_method_types[]':'us_bank_account'},key=key)
        self.assertEqual(status,200,intent)
        status, processing, _ = self.request('POST','/v1/payment_intents/' + intent['id'] + '/confirm',
            {'payment_method':'pm_usBankAccount_processing'},key=key+'-confirm')
        self.assertEqual(status,200,processing)
        self.assertEqual(processing['status'],'processing')
        self.assertIsNone(processing['latest_charge'])
        return processing

    def test_bank_payment_settles_only_after_explicit_resolution(self):
        intent = self.create('bank-success')
        info = self.control('resources/inspect',{'kind':'payment_intent','resource_id':intent['id']})[1]
        action = {'action':'succeed','expected_provider_version':info['provider_version']}
        endpoint = 'payment_intents/' + intent['id'] + '/bank_actions'
        self.assertEqual(self.control(endpoint,action)[0],200)
        status, paid, _ = self.request('GET','/v1/payment_intents/' + intent['id'])
        self.assertEqual(status,200,paid)
        self.assertEqual(paid['status'],'succeeded')
        self.assertEqual(paid['latest_charge']['payment_method_details']['type'],'us_bank_account')
        self.assertEqual(self.control(endpoint,action)[0],409)
        events = [json.loads(bytes(row['payload']))['type'] for row in self.store.events(self.run)]
        self.assertIn('payment_intent.processing',events)
        self.assertIn('payment_intent.succeeded',events)

    def test_failed_bank_payment_has_no_charge(self):
        intent = self.create('bank-failure')
        info = self.control('resources/inspect',{'kind':'payment_intent','resource_id':intent['id']})[1]
        status, result, _ = self.control('payment_intents/' + intent['id'] + '/bank_actions',
            {'action':'fail','expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,result)
        failed = self.request('GET','/v1/payment_intents/' + intent['id'])[1]
        self.assertEqual(failed['status'],'requires_payment_method')
        self.assertIsNone(failed['latest_charge'])
        self.assertEqual(failed['last_payment_error']['code'],'debit_not_authorized')

    def test_microdeposit_verification_and_synthetic_mandate(self):
        status, intent, _ = self.request('POST','/v1/payment_intents',{
            'amount':'1200','currency':'usd','allowed_payment_method_types[]':'us_bank_account'},key='verify')
        self.assertEqual(status,200,intent)
        path = '/v1/payment_intents/' + intent['id']
        status, pending, _ = self.request('POST',path+'/confirm',
            {'payment_method':'pm_usBankAccount_unverified'},key='verify-confirm')
        self.assertEqual(status,200,pending)
        self.assertEqual(pending['status'],'requires_action')
        mandate = pending['mandate']
        self.assertEqual(self.request('GET','/v1/mandates/' + mandate)[1]['status'],'pending')
        status, error, _ = self.request('POST',path+'/verify_microdeposits',{'descriptor_code':'SM0000'},key='bad-code')
        self.assertEqual(status,400,error)
        self.assertEqual(error['error']['code'],'payment_method_microdeposit_verification_amounts_mismatch')
        status, verified, _ = self.request('POST',path+'/verify_microdeposits',{'descriptor_code':'SM11AA'},key='good-code')
        self.assertEqual(status,200,verified)
        self.assertEqual(verified['status'],'processing')
        self.assertEqual(self.request('GET','/v1/mandates/' + mandate)[1]['status'],'active')
        info = self.control('resources/inspect',{'kind':'payment_intent','resource_id':intent['id']})[1]
        self.assertEqual(self.control('payment_intents/' + intent['id'] + '/bank_actions',
            {'action':'succeed','expected_provider_version':info['provider_version']})[0],200)

    def test_dispute_creation_and_resolution(self):
        intent = self.create('bank-dispute')
        info = self.control('resources/inspect',{'kind':'payment_intent','resource_id':intent['id']})[1]
        self.assertEqual(self.control('payment_intents/' + intent['id'] + '/bank_actions',
            {'action':'succeed','expected_provider_version':info['provider_version']})[0],200)
        charge = self.request('GET','/v1/payment_intents/' + intent['id'])[1]['latest_charge']['id']
        charge_info = self.control('resources/inspect',{'kind':'charge','resource_id':charge})[1]
        status, created, _ = self.control('charges/' + charge + '/disputes',
            {'expected_provider_version':charge_info['provider_version']})
        self.assertEqual(status,200,created)
        status, dispute, _ = self.request('GET','/v1/disputes/' + created['dispute'])
        self.assertEqual(status,200,dispute)
        self.assertEqual((dispute['charge'],dispute['status']),(charge,'needs_response'))
        listed = self.request('GET','/v1/disputes?charge=' + charge)[1]
        self.assertEqual(len(listed['data']),1)
        info = self.control('resources/inspect',{'kind':'dispute','resource_id':dispute['id']})[1]
        status, result, _ = self.control('disputes/' + dispute['id'] + '/actions',
            {'action':'win','expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,result)
        self.assertEqual(self.request('GET','/v1/disputes/' + dispute['id'])[1]['status'],'won')

    def test_verification_failures_are_bounded_and_idempotent(self):
        status, intent, _ = self.request('POST','/v1/payment_intents',{
            'amount':'1200','currency':'usd','allowed_payment_method_types[]':'us_bank_account'},key='limit')
        self.assertEqual(status,200,intent)
        path = '/v1/payment_intents/' + intent['id']
        pending = self.request('POST',path+'/confirm',{'payment_method':'pm_usBankAccount_unverified'},key='limit-confirm')[1]
        for index in range(3):
            key = 'wrong-' + str(index)
            status, response, _ = self.request('POST',path+'/verify_microdeposits',{'descriptor_code':'SM0000'},key=key)
            self.assertEqual(status,400,response)
            self.assertEqual(self.request('POST',path+'/verify_microdeposits',{'descriptor_code':'SM0000'},key=key)[1],response)
        self.assertEqual(response['error']['code'],'payment_method_microdeposit_verification_attempts_exceeded')
        self.assertEqual(self.request('GET',path)[1]['status'],'requires_payment_method')
        self.assertEqual(self.request('GET','/v1/mandates/' + pending['mandate'])[1]['status'],'inactive')
        self.assertEqual(self.request('POST',path+'/verify_microdeposits',{'descriptor_code':'SM11AA'},key='too-late')[0],409)

    def test_unauthorized_bank_payment_revokes_its_mandate(self):
        status, intent, _ = self.request('POST','/v1/payment_intents',{
            'amount':'1200','currency':'usd','allowed_payment_method_types[]':'us_bank_account'},key='revoke')
        self.assertEqual(status,200,intent)
        path = '/v1/payment_intents/'+intent['id']
        pending = self.request('POST',path+'/confirm',{'payment_method':'pm_usBankAccount_unverified'},key='revoke-confirm')[1]
        self.assertEqual(self.request('POST',path+'/verify_microdeposits',{'descriptor_code':'SM11AA'},key='revoke-verify')[0],200)
        info = self.control('resources/inspect',{'kind':'payment_intent','resource_id':intent['id']})[1]
        self.assertEqual(self.control('payment_intents/'+intent['id']+'/bank_actions',
            {'action':'fail','expected_provider_version':info['provider_version']})[0],200)
        self.assertEqual(self.request('GET','/v1/mandates/'+pending['mandate'])[1]['status'],'inactive')
        status, processing, _ = self.request('POST',path+'/confirm',{'payment_method':'pm_usBankAccount_processing'},key='new-method')
        self.assertEqual(status,200,processing)
        self.assertFalse(processing.get('mandate'))

    @patch('stripe_double.wire.MAX_EVENTS',4)
    @patch('stripe_double.state.MAX_ACCOUNTING_OBJECTS',2)
    def test_capacity_is_reserved_to_resolve_processing_bank_payment(self):
        intent = self.create('capacity-bank')
        status, _, _ = self.control('actions',{'action':'duplicate_last_event',
            'expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,429)
        info = self.control('resources/inspect',{'kind':'payment_intent','resource_id':intent['id']})[1]
        status, result, _ = self.control('payment_intents/'+intent['id']+'/bank_actions',
            {'action':'succeed','expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,result)

    @patch('stripe_double.wire.MAX_EVENTS',6)
    @patch('stripe_double.state.MAX_ACCOUNTING_OBJECTS',2)
    def test_capacity_carries_across_verification_and_bank_settlement(self):
        intent = self.request('POST','/v1/payment_intents',{'amount':'1000','currency':'usd',
            'allowed_payment_method_types[]':'us_bank_account'},key='verify-capacity')[1]
        path = '/v1/payment_intents/'+intent['id']
        self.assertEqual(self.request('POST',path+'/confirm',{'payment_method':'pm_usBankAccount_unverified'},
            key='verify-capacity-confirm')[0],200)
        self.assertEqual(self.request('POST',path+'/verify_microdeposits',{'descriptor_code':'SM11AA'},
            key='verify-capacity-code')[0],200)
        info = self.control('resources/inspect',{'kind':'payment_intent','resource_id':intent['id']})[1]
        self.assertEqual(self.control('payment_intents/'+intent['id']+'/bank_actions',
            {'action':'succeed','expected_provider_version':info['provider_version']})[0],200)

    @patch('stripe_double.wire.MAX_OBJECTS',4)
    def test_resource_capacity_reserves_room_for_processing_bank_charge(self):
        intent = self.create('resource-bank')
        self.assertEqual(self.request('POST','/v1/payment_intents',{'amount':'1000','currency':'usd'},key='other-one')[0],200)
        self.assertEqual(self.request('POST','/v1/payment_intents',{'amount':'1000','currency':'usd'},key='other-two')[0],429)
        info = self.control('resources/inspect',{'kind':'payment_intent','resource_id':intent['id']})[1]
        self.assertEqual(self.control('payment_intents/'+intent['id']+'/bank_actions',
            {'action':'succeed','expected_provider_version':info['provider_version']})[0],200)
