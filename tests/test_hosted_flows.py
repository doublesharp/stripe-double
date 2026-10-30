"""Local hosted Checkout, portal and OAuth over real HTTP."""
import http.client
import re
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

import test_checkout
import test_near_term
import test_http


class HostedFlowsTests(unittest.TestCase):
    setUp = test_checkout.CheckoutTests.setUp
    close = test_checkout.CheckoutTests.close
    request = test_checkout.CheckoutTests.request
    control = test_checkout.CheckoutTests.control
    seed = test_checkout.CheckoutTests.seed
    account = test_near_term.NearTermHttpTests.account
    admin = test_near_term.NearTermHttpTests.admin
    ready = test_near_term.NearTermHttpTests.ready

    def browser(self, method, url, values=None, *, origin=True):
        target = urlsplit(url)
        self.assertEqual((target.hostname,target.port),self.server.address)
        connection = http.client.HTTPConnection(*self.server.address,timeout=3)
        body = urlencode(values or {}) if method == 'POST' else None
        headers = {}
        if body is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            if origin:
                headers['Origin'] = f'http://{self.server.address[0]}:{self.server.address[1]}'
        try:
            connection.request(method,target.path + ('?' + target.query if target.query else ''),body,headers)
            response = connection.getresponse()
            return response.status,response.read().decode(),dict(response.getheaders())
        finally:
            connection.close()

    def input(self, html, name):
        match = re.search(r'name="' + re.escape(name) + r'" value="([^"]+)"',html)
        self.assertIsNotNone(match,html)
        return match.group(1)

    def test_hosted_checkout_completes_and_redirects_to_local_success(self):
        seed = self.seed()
        form = {'mode':'subscription','customer':seed['customer'],
                'line_items[0][price]':seed['price'],'line_items[0][quantity]':'1',
                'success_url':'http://127.0.0.1:3000/success?session={CHECKOUT_SESSION_ID}',
                'cancel_url':'http://127.0.0.1:3000/cancel'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='hosted')
        self.assertEqual(status,200,session)
        self.assertEqual(urlsplit(session['url']).hostname,'127.0.0.1')
        status, page, headers = self.browser('GET',session['url'])
        self.assertEqual(status,200,page)
        self.assertIn('Synthetic checkout',page)
        self.assertEqual(headers['Referrer-Policy'],'strict-origin')
        csrf, version = self.input(page,'_csrf'),self.input(page,'version')
        self.assertNotIn(csrf,self.path.read_bytes().decode('latin1'))
        self.assertEqual(self.browser('POST',session['url'],{'_csrf':'é','version':version,'action':'complete'})[0],403)
        self.assertEqual(self.browser('POST',session['url'],{'_csrf':csrf,'version':version,'action':'complete'},origin=False)[0],403)
        status, _, headers = self.browser('POST',session['url'],{'_csrf':csrf,'version':version,'action':'complete'})
        self.assertEqual(status,303)
        self.assertEqual(headers['Location'],'http://127.0.0.1:3000/success?session='+session['id'])
        self.assertEqual(self.request('GET','/v1/checkout/sessions/'+session['id'])[1]['status'],'complete')
        self.assertNotEqual(self.browser('POST',session['url'],{'_csrf':csrf,'version':version,'action':'complete'})[0],303)

    def test_portal_schedules_cancellation_for_own_customer(self):
        seed = self.seed()
        status, portal, _ = self.request('POST','/v1/billing_portal/sessions',
            {'customer':seed['customer'],'return_url':'http://127.0.0.1:3000/account'},key='portal-hosted')
        self.assertEqual(status,200,portal)
        status, page, _ = self.browser('GET',portal['url'])
        self.assertEqual(status,200,page)
        self.assertIn('Synthetic customer portal',page)
        status, _, _ = self.browser('POST',portal['url'],{'_csrf':self.input(page,'_csrf'),
            'version':self.input(page,'version'),'subscription':seed['subscription'],'action':'cancel'})
        self.assertEqual(status,303)
        subscription = self.request('GET','/v1/subscriptions/'+seed['subscription'])[1]
        self.assertEqual(subscription['status'],'active')
        self.assertTrue(subscription['cancel_at_period_end'])
        version = self.store.object(self.run,'subscription',seed['subscription'])['version']
        action = {'action':'renew','expected_provider_version':version}
        self.assertEqual(self.control('billing/subscriptions/'+seed['subscription']+'/actions',action)[0],409)
        self.clock.advance(subscription['current_period_end']-int(self.clock())+1)
        self.assertEqual(self.control('billing/subscriptions/'+seed['subscription']+'/actions',action)[1]['status'],'canceled')

    def test_oauth_approval_code_is_one_use_and_scoped(self):
        account = self.account()
        self.ready()
        origin = f'http://{self.server.address[0]}:{self.server.address[1]}'
        query = urlencode({'client_id':'ca_LocalSim'+self.store.instance_id.replace('-',''),
            'response_type':'code','scope':'read_write','state':'state_example',
            'redirect_uri':'http://127.0.0.1:3000/callback','run':self.run})
        url = origin+'/oauth/authorize?'+query
        status, page, _ = self.browser('GET',url)
        self.assertEqual(status,200,page)
        action = re.search(r'<form method="post" action="([^"]+)"',page).group(1)
        status, _, headers = self.browser('POST',origin+action,{'_csrf':self.input(page,'_csrf'),
            'account':account,'action':'approve'})
        self.assertEqual(status,303)
        response = urlsplit(headers['Location'])
        query = parse_qs(response.query)
        self.assertEqual(query['state'],['state_example'])
        code = query['code'][0]
        status, token, _ = self.request('POST','/oauth/token',{'grant_type':'authorization_code','code':code},key='exchange')
        self.assertEqual(status,200,token)
        self.assertEqual((token['stripe_user_id'],token['scope'],token['livemode']),(account,'read_write',False))
        self.assertEqual(self.request('POST','/oauth/token',{'grant_type':'authorization_code','code':code},key='exchange')[0],400)
        access = token['access_token']
        self.assertNotIn(access,self.path.read_bytes().decode('latin1'))
        self.assertEqual(self.request('GET','/v1/accounts/'+account,token=access)[1]['type'],'standard')
        status, intent, _ = self.request('POST','/v1/payment_intents',test_http.FIXTURE['intent_form'],account=account,key='oauth-intent')
        self.assertEqual(status,200,intent)
        status, intent, _ = self.request('GET','/v1/payment_intents/'+intent['id'],token=access)
        self.assertEqual(status,200,intent)
        status, _, _ = self.request('POST','/__simulator/v1/components/exchange',
            {'client_secret':intent['client_secret'],'account':account,'purpose':'card_payment'},json_body=True)
        self.assertEqual(status,200)
        other = self.account('other')
        self.assertEqual(self.request('GET','/v1/accounts/'+other,token=access)[0],403)
        self.assertEqual(self.request('GET','/v1/balance',token=access,account=other)[0],403)
        another_run = self.store.create_run('STRIPE-LOCAL-01')['simulation_run_id']
        self.assertEqual(self.request('GET','/v1/balance',token=access,run=another_run)[0],403)
        self.clock.advance(3601)
        self.assertEqual(self.request('GET','/v1/accounts/'+account,token=access)[0],403)

    def test_remote_return_is_rejected_before_checkout_mutation(self):
        seed = self.seed()
        _, session, _ = self.request('POST','/v1/checkout/sessions',
            {'mode':'subscription','customer':seed['customer'],'line_items[0][price]':seed['price'],
             'line_items[0][quantity]':'1','success_url':'https://example.com/success',
             'cancel_url':'http://127.0.0.1:3000/cancel'},key='remote')
        _, page, _ = self.browser('GET',session['url'])
        status, _, _ = self.browser('POST',session['url'],{'_csrf':self.input(page,'_csrf'),
            'version':self.input(page,'version'),'action':'complete'})
        self.assertEqual(status,400)
        self.assertEqual(self.request('GET','/v1/checkout/sessions/'+session['id'])[1]['status'],'open')

    def test_portal_rejects_another_customers_subscription(self):
        first, second = self.seed(), self.seed()
        _, portal, _ = self.request('POST','/v1/billing_portal/sessions',
            {'customer':first['customer'],'return_url':'http://127.0.0.1:3000/account'},key='portal-scope')
        _, page, _ = self.browser('GET',portal['url'])
        version = self.store.object(self.run,'subscription',second['subscription'])['version']
        status, _, _ = self.browser('POST',portal['url'],{'_csrf':self.input(page,'_csrf'),
            'version':version,'subscription':second['subscription'],'action':'cancel'})
        self.assertEqual(status,403)
        self.assertFalse(self.store.object(self.run,'subscription',second['subscription']).get('cancel_at_period_end',False))

    def test_checkout_capability_expires_and_api_read_renews_it(self):
        seed = self.seed()
        _, session, _ = self.request('POST','/v1/checkout/sessions',
            {'mode':'subscription','customer':seed['customer'],'line_items[0][price]':seed['price'],
             'line_items[0][quantity]':'1','success_url':'http://127.0.0.1:3000/success',
             'cancel_url':'http://127.0.0.1:3000/cancel'},key='expiring')
        old = session['url']
        self.clock.advance(301)
        self.assertEqual(self.browser('GET',old)[0],403)
        _, session, _ = self.request('GET','/v1/checkout/sessions/'+session['id'])
        self.assertNotEqual(session['url'],old)
        self.assertEqual(self.browser('GET',session['url'])[0],200)
        listing = self.request('GET','/v1/checkout/sessions')[1]
        listed = next(item for item in listing['data'] if item['id'] == session['id'])
        self.assertEqual(listed['url'],session['url'])
        version = self.store.run(self.run)['state_version']
        self.assertEqual(self.control('reset',{'expected_state_version':version})[0],200)
        self.assertEqual(self.browser('GET',session['url'])[0],403)

    def test_capacity_refusal_precedes_durable_session_creation(self):
        seed = self.seed()
        for index in range(1024):
            self.server.hosted.issue('access',self.run,resource=str(index))
        before = len(self.store.objects(self.run,'portal_session'))
        form = {'customer':seed['customer'],'return_url':'http://127.0.0.1:3000/account'}
        self.assertEqual(self.request('POST','/v1/billing_portal/sessions',form,key='capacity')[0],429)
        self.assertEqual(len(self.store.objects(self.run,'portal_session')),before)
        self.server.hosted.close()
        self.assertEqual(self.request('POST','/v1/billing_portal/sessions',form,key='capacity')[0],200)

    def test_oauth_denial_without_any_accounts(self):
        origin = f'http://{self.server.address[0]}:{self.server.address[1]}'
        query = urlencode({'client_id':'ca_LocalSim'+self.store.instance_id.replace('-',''),
            'response_type':'code','scope':'read_write','state':'state_example',
            'redirect_uri':'http://127.0.0.1:3000/callback','run':self.run})
        _, page, _ = self.browser('GET',origin+'/oauth/authorize?'+query)
        action = re.search(r'<form method="post" action="([^"]+)"',page).group(1)
        status, _, headers = self.browser('POST',origin+action,{'_csrf':self.input(page,'_csrf'),'action':'deny'})
        self.assertEqual(status,303)
        self.assertEqual(parse_qs(urlsplit(headers['Location']).query),{'state':['state_example'],'error':['access_denied']})

    def test_pending_authorizations_do_not_block_approval_or_session_read(self):
        seed, account = self.seed(), self.account()
        _, session, _ = self.request('POST','/v1/checkout/sessions',
            {'mode':'subscription','customer':seed['customer'],'line_items[0][price]':seed['price'],
             'line_items[0][quantity]':'1','success_url':'http://127.0.0.1:3000/success',
             'cancel_url':'http://127.0.0.1:3000/cancel'},key='pending')
        self.clock.advance(301)
        for _ in range(1023):
            self.server.hosted.issue('oauth',self.run)
        origin = f'http://{self.server.address[0]}:{self.server.address[1]}'
        query = urlencode({'client_id':'ca_LocalSim'+self.store.instance_id.replace('-',''),
            'response_type':'code','scope':'read_write','state':'state_example',
            'redirect_uri':'http://127.0.0.1:3000/callback','run':self.run})
        _, page, _ = self.browser('GET',origin+'/oauth/authorize?'+query)
        action = re.search(r'<form method="post" action="([^"]+)"',page).group(1)
        status, _, headers = self.browser('POST',origin+action,{'_csrf':self.input(page,'_csrf'),
            'account':account,'action':'approve'})
        self.assertEqual(status,303)
        self.assertIn('code',parse_qs(urlsplit(headers['Location']).query))
        self.assertEqual(self.request('GET','/v1/checkout/sessions/'+session['id'])[0],200)

    def test_old_checkout_without_return_fields_fails_closed(self):
        seed = self.seed()
        _, session, _ = self.request('POST','/v1/checkout/sessions',
            {'mode':'subscription','customer':seed['customer'],'line_items[0][price]':seed['price'],
             'line_items[0][quantity]':'1','success_url':'http://127.0.0.1:3000/success',
             'cancel_url':'http://127.0.0.1:3000/cancel'},key='old-session')
        with self.store.transaction():
            doc = self.store._object(self.run,'checkout_session',session['id'])
            doc.pop('local_success_url')
            doc.pop('local_cancel_url')
            self.store._put(self.run,self.store.platform_account,'checkout_session',doc)
        _, page, _ = self.browser('GET',session['url'])
        status, _, _ = self.browser('POST',session['url'],{'_csrf':self.input(page,'_csrf'),
            'version':self.input(page,'version'),'action':'complete'})
        self.assertEqual(status,400)
        self.assertEqual(self.request('GET','/v1/checkout/sessions/'+session['id'])[1]['status'],'open')
