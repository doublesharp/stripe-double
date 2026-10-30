"""Optional browser proof against the real loopback server, with no mocks."""
import http.server
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

import test_hosted_flows


@unittest.skipUnless(os.environ.get('STRIPE_DOUBLE_PLAYWRIGHT_MODULE'), 'Playwright module not configured')
class BrowserTests(unittest.TestCase):
    setUp = test_hosted_flows.HostedFlowsTests.setUp
    close = test_hosted_flows.HostedFlowsTests.close
    request = test_hosted_flows.HostedFlowsTests.request
    control = test_hosted_flows.HostedFlowsTests.control
    seed = test_hosted_flows.HostedFlowsTests.seed
    account = test_hosted_flows.HostedFlowsTests.account

    def test_checkout_portal_and_oauth_real_browser_journeys(self):
        class Callback(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length','2')
                self.end_headers()
                self.wfile.write(b'OK')
            def log_message(self,*args):
                pass
        callback = http.server.ThreadingHTTPServer(('127.0.0.1',0),Callback)
        thread = threading.Thread(target=callback.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(callback.server_close)
        self.addCleanup(callback.shutdown)
        base = 'http://127.0.0.1:'+str(callback.server_port)
        seed, account = self.seed(), self.account()
        _, checkout, _ = self.request('POST','/v1/checkout/sessions',{
            'mode':'subscription','customer':seed['customer'],'line_items[0][price]':seed['price'],
            'line_items[0][quantity]':'1','success_url':base+'/success?session={CHECKOUT_SESSION_ID}',
            'cancel_url':base+'/cancel'},key='browser-checkout')
        _, portal, _ = self.request('POST','/v1/billing_portal/sessions',
            {'customer':seed['customer'],'return_url':base+'/account'},key='browser-portal')
        origin = 'http://127.0.0.1:'+str(self.server.address[1])
        oauth = origin+'/oauth/authorize?'+urlencode({'client_id':'ca_LocalSim'+self.store.instance_id.replace('-',''),
            'response_type':'code','scope':'read_write','state':'browser_state','redirect_uri':base+'/oauth','run':self.run})
        proc = subprocess.run(['node',str(Path(__file__).with_name('hosted_browser.cjs'))],
            input=json.dumps({'checkout':checkout['url'],'portal':portal['url'],'oauth':oauth,
                              'account':account,'subscription':seed['subscription'],'callback':base}),text=True,capture_output=True,timeout=45)
        self.assertEqual(proc.returncode,0,proc.stderr)
        results = json.loads(proc.stdout)
        self.assertEqual(parse_qs(urlsplit(results['checkout']).query)['session'],[checkout['id']])
        self.assertEqual(self.request('GET','/v1/checkout/sessions/'+checkout['id'])[1]['status'],'complete')
        self.assertTrue(self.request('GET','/v1/subscriptions/'+seed['subscription'])[1]['cancel_at_period_end'])
        params = parse_qs(urlsplit(results['oauth']).query)
        self.assertEqual(params['state'],['browser_state'])
        self.assertEqual(self.request('POST','/oauth/token',{'grant_type':'authorization_code','code':params['code'][0]},key='browser-code')[1]['stripe_user_id'],account)
