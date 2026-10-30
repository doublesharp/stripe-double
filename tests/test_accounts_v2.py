"""Bounded Accounts v2 merchant identity and requirement simulation."""
import unittest
import test_near_term


class AccountsV2Tests(unittest.TestCase):
    setUp = test_near_term.NearTermHttpTests.setUp
    close = test_near_term.NearTermHttpTests.close
    request = test_near_term.NearTermHttpTests.request
    control = test_near_term.NearTermHttpTests.control

    def test_synthetic_identity_requirements_can_be_resolved(self):
        form = {'contact_email':'owner@example.test','display_name':'Synthetic Merchant',
                'identity':{'country':'us','entity_type':'company'},
                'configuration':{'merchant':{'capabilities':{'card_payments':{'requested':True}}}},
                'include':['identity','configuration.merchant','requirements']}
        status, account, _ = self.request('POST','/v2/core/accounts',form,key='v2-create',json_body=True)
        self.assertEqual(status,200,account)
        self.assertEqual(account['object'],'v2.core.account')
        self.assertEqual(account['identity']['country'],'us')
        self.assertEqual(account['configuration']['merchant']['capabilities']['card_payments']['status'],'restricted')
        self.assertEqual(account['requirements']['entries'][0]['minimum_deadline']['status'],'past_due')
        info = self.control('resources/inspect',{'kind':'account_v2','resource_id':account['id'],'account':account['id']})[1]
        status, result, _ = self.control('accounts_v2/' + account['id'] + '/identity',
            {'action':'verify','expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,result)
        status, observed, _ = self.request('GET','/v2/core/accounts/' + account['id'] +
            '?include%5B0%5D=identity&include%5B1%5D=configuration.merchant&include%5B2%5D=requirements')
        self.assertEqual(status,200,observed)
        self.assertEqual(observed['configuration']['merchant']['capabilities']['card_payments']['status'],'active')
        self.assertEqual(observed['requirements']['entries'],[])
        self.assertEqual(self.control('accounts_v2/' + account['id'] + '/identity',
            {'action':'verify','expected_provider_version':info['provider_version']})[0],409)

    def test_v2_refuses_real_identity_data(self):
        form = {'contact_email':'person@example.com','identity':{'country':'us','entity_type':'individual'},
                'configuration':{'merchant':{'capabilities':{'card_payments':{'requested':True}}}}}
        self.assertEqual(self.request('POST','/v2/core/accounts',form,key='real',json_body=True)[0],400)
        form['contact_email'] = 'person@example.test'
        form['identity']['entity_type'] = {'unexpected':'object'}
        self.assertEqual(self.request('POST','/v2/core/accounts',form,key='malformed',json_body=True)[0],400)

    def test_v2_identity_transition_queues_thin_events_with_canonical_resource(self):
        form = {'contact_email':'owner@example.test','display_name':'Synthetic Merchant',
                'identity':{'country':'us','entity_type':'company'},
                'configuration':{'merchant':{'capabilities':{'card_payments':{'requested':True}}}}}
        status, account, _ = self.request('POST','/v2/core/accounts',form,key='v2-events',json_body=True)
        self.assertEqual(status,200,account)
        info = self.control('resources/inspect',{'kind':'account_v2','resource_id':account['id'],'account':account['id']})[1]
        self.assertEqual(self.control('accounts_v2/' + account['id'] + '/identity',
            {'action':'verify','expected_provider_version':info['provider_version']})[0],200)
        status, page, _ = self.control('events/list',{})
        self.assertEqual(status,200,page)
        self.assertIn('v2.core.account[requirements].updated',[e['type'] for e in page['events']])
        event = next(e for e in page['events'] if e['type'] == 'v2.core.account[requirements].updated')
        self.assertEqual(event['resource_id'],account['id'])
        status, notification, _ = self.request('GET','/v2/core/events/' + event['event_id'])
        self.assertEqual(status,200,notification)
        self.assertEqual(notification['object'],'v2.core.event')
        self.assertEqual(notification['related_object']['id'],account['id'])
        self.assertEqual(notification['related_object']['url'],'/v2/core/accounts/' + account['id'])
        self.assertNotIn('data',notification)
