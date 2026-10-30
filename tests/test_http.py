"""Actual owned HTTP transport, state and lifecycle tests."""
from pathlib import Path
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import select
import subprocess
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

from stripe_double.http_api import SimulatorServer, Credentials
from stripe_double.state import StateStore
from stripe_double.wire import API_VERSION, SimulatorError, MAX_BODY
from test_state import Clock, case_path, raw_form, FIXTURE


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.path = case_path(self)
        self.store = StateStore(self.path, create=True, clock=self.clock)
        self.server = SimulatorServer(self.store, clock=self.clock)
        self.server.start()
        self.addCleanup(self.close)
        self.run = self.store.create_run("STRIPE-LOCAL-01")["simulation_run_id"]

    def close(self):
        self.server.close()
        self.store.close()

    def request(self, method, path, values=None, *, account=None, key=None, run=None,
                token=None, raw=None, headers=None, json_body=False):
        body = raw if raw is not None else (json.dumps(values or {}).encode() if json_body else raw_form(values or {}))
        head = {
            "Authorization":"Bearer " + (token or self.server.credentials.api_key),
            "Stripe-Version":API_VERSION,
            "X-Local-Simulator-Instance":self.store.instance_id,
            "X-Local-Simulator-Run":run or self.run,
            "Content-Type":"application/json" if json_body else "application/x-www-form-urlencoded",
        }
        if account is not None:
            head["Stripe-Account"] = account
        if key is not None:
            head["Idempotency-Key"] = key
        head.update(headers or {})
        c = http.client.HTTPConnection(*self.server.address, timeout=3)
        try:
            c.request(method, path, body, head)
            response = c.getresponse()
            data = response.read()
            self.assertLessEqual(len(data), MAX_BODY)
            return response.status, json.loads(data), dict(response.getheaders())
        finally:
            c.close()

    def admin(self, action, **extra):
        values = {"action":action,"expected_state_version":self.store.run(self.run)["state_version"], **extra}
        return self.request("POST", "/__simulator/v1/runs/" + self.run + "/actions", values,
                            token=self.server.credentials.admin_token, json_body=True)

    def account(self, key="account"):
        status, doc, _ = self.request("POST","/v1/accounts",FIXTURE["account_form"],key=key)
        self.assertEqual(status,200,doc)
        return doc["id"]

    def ready(self):
        status, doc, _ = self.admin("requirements_complete")
        self.assertEqual(status,200,doc)

    def session(self, account, purpose="onboarding"):
        status, doc, _ = self.request("POST", "/v1/account_sessions",
                                     {"account":account,**FIXTURE["session_forms"][purpose]})
        self.assertEqual(status,200,doc)
        return doc

    def exchange(self, secret, account, purpose="onboarding", run=None):
        return self.request("POST", "/__simulator/v1/components/exchange",
            {"client_secret":secret,"account":account,"purpose":purpose}, json_body=True,run=run)

    def component(self, cap, account, purpose, action, version):
        return self.request("POST", "/__simulator/v1/components/actions",
            {"control_capability":cap,"account":account,"purpose":purpose,"action":action,
             "expected_provider_version":version}, json_body=True)

    def restart(self):
        self.server.close()
        self.store.close()
        self.store = StateStore(self.path, clock=self.clock)
        self.server = SimulatorServer(self.store, clock=self.clock)
        self.server.start()

    def payment(self):
        account = self.account()
        self.ready()
        status, pi, _ = self.request("POST","/v1/payment_intents",FIXTURE["intent_form"],account=account,key="pi")
        self.assertEqual(status,200,pi)
        status, ui, _ = self.request("GET","/v1/payment_intents/" + pi["id"] + "?expand%5B%5D=latest_charge",account=account)
        self.assertEqual(status,200,ui)
        self.assertTrue(ui["client_secret"].startswith(pi["id"] + "_secret_"))
        status, cap, _ = self.exchange(ui["client_secret"],account,"card_payment")
        self.assertEqual(status,200,cap)
        status, result, _ = self.component(cap["control_capability"],account,"card_payment","confirm",cap["provider_version"])
        self.assertEqual(status,200,result)
        return account, pi["id"]

    def test_default_profile_accepts_current_stripe_version_and_tags_events(self):
        latest = "2026-10-28.endive"
        status, account, _ = self.request("POST", "/v1/accounts", FIXTURE["account_form"],
            key="current-account", headers={"Stripe-Version":latest})
        self.assertEqual(status, 200, account)
        events = [json.loads(bytes(row["payload"])) for row in self.store.events(self.run)]
        self.assertEqual(events[0]["api_version"], latest)
        self.assertEqual(self.request("GET", "/v1/accounts/" + account["id"],
            headers={"Stripe-Version":"2025-02-24.acacia"})[0], 400)

    def test_latest_profile_rejects_removed_payment_method_types(self):
        account = self.account()
        self.ready()
        form = dict(FIXTURE["intent_form"])
        form.pop("allowed_payment_method_types[]", None)
        form["payment_method_types[]"] = "card"
        status, error, _ = self.request("POST", "/v1/payment_intents", form,
            account=account, key="removed-parameter")
        self.assertEqual(status, 400, error)
        self.assertEqual(error["error"]["code"], "payment_method_types_no_longer_supported")
        self.assertEqual(self.store.objects(self.run, "payment_intent"), [])
        form.pop("payment_method_types[]")
        form["allowed_payment_method_types[]"] = "card"
        status, intent, _ = self.request("POST", "/v1/payment_intents", form,
            account=account, key="removed-parameter")
        self.assertEqual(status, 200, intent)
        self.assertEqual(intent["payment_method_types"], ["card"])

    def test_current_profile_defaults_to_card_and_refuses_unmodelled_methods(self):
        account = self.account()
        self.ready()
        form = dict(FIXTURE["intent_form"])
        form.pop("allowed_payment_method_types[]")
        status, intent, _ = self.request("POST", "/v1/payment_intents", form,
            account=account, key="dynamic-card")
        self.assertEqual(status, 200, intent)
        self.assertEqual(intent["payment_method_types"], ["card"])
        status, _, _ = self.request("POST", "/v1/payment_intents/" + intent["id"] + "/cancel", {},
            account=account, key="cancel-dynamic-card")
        self.assertEqual(status, 200)
        self.assertEqual(self.store.objects(self.run, "refund"), [])
        form["allowed_payment_method_types[]"] = "us_bank_account"
        status, error, _ = self.request("POST", "/v1/payment_intents", form,
            account=account, key="unsupported-method")
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "simulator_unsupported")
        self.assertEqual(len(self.store.objects(self.run, "payment_intent")), 1)

    def test_expiry_cannot_overtake_an_admitted_component_action(self):
        account = self.account()
        cap = self.exchange(self.session(account)["client_secret"],account)[1]
        admitted = threading.Event()
        release = threading.Event()
        expired = threading.Event()
        results,errors = [],[]
        action = self.store.component_action
        def held(*args):
            admitted.set()
            if not release.wait(2): raise RuntimeError("test gate timeout")
            return action(*args)
        def perform():
            try: results.append(self.component(cap["control_capability"],account,"onboarding","requirements_complete",cap["provider_version"])[0])
            except BaseException as error: errors.append(type(error).__name__)
        def expire():
            try: results.append(self.admin("expire_sessions")[0])
            except BaseException as error: errors.append(type(error).__name__)
            finally: expired.set()
        workers = []
        try:
            with patch.object(self.store,"component_action",held):
                worker = threading.Thread(target=perform);workers.append(worker);worker.start()
                self.assertTrue(admitted.wait(1))
                worker = threading.Thread(target=expire);workers.append(worker);worker.start()
                self.assertFalse(expired.wait(.1),"expiry acknowledged while admitted action can still mutate")
                release.set()
                for worker in workers: worker.join(3)
        finally:
            release.set()
            for worker in workers: worker.join(3)
        self.assertEqual(errors,[])
        self.assertEqual(results,[200,200])
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(self.component(cap["control_capability"],account,"onboarding","exit",cap["provider_version"])[0],403)

    def test_lost_capture_and_fee_refund_replay_after_restart(self):
        account,pi = self.payment()
        capture_form = FIXTURE["capture_form"]
        self.assertEqual(self.admin("drop_next_reply_after_commit")[0],200)
        with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):
            self.request("POST","/v1/payment_intents/"+pi+"/capture",capture_form,account=account,key="lost-capture")
        self.restart()
        status,captured,_ = self.request("POST","/v1/payment_intents/"+pi+"/capture",capture_form,account=account,key="lost-capture")
        self.assertEqual(status,200,captured)
        self.assertEqual(len(self.store.objects(self.run,"charge")),1)
        fee = captured["latest_charge"]["application_fee"]
        self.assertEqual(self.admin("drop_next_reply_after_commit")[0],200)
        values = {"amount":"100","metadata[refund_id]":"original"}
        with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):
            self.request("POST","/v1/application_fees/"+fee+"/refunds",values,key="lost-fee")
        self.restart()
        status,refund,_ = self.request("POST","/v1/application_fees/"+fee+"/refunds",values,key="lost-fee")
        self.assertEqual(status,200,refund)
        self.assertEqual(len(self.store.objects(self.run,"fee_refund")),1)
        self.assertEqual(self.request("GET","/v1/application_fees/"+fee+"/refunds/"+refund["id"])[1],refund)

    def test_capability_capacity_and_no_callback_from_control_request(self):
        account = self.account()
        for _ in range(32): self.session(account)
        status,doc,_ = self.request("POST","/v1/account_sessions",{"account":account,**FIXTURE["session_forms"]["onboarding"]})
        self.assertEqual(status,429,doc)
        self.assertEqual(self.admin("expire_sessions")[0],200)
        self.session(account)
        attempts = []
        def record(*args,**kwargs):
            attempts.append(args)
            raise OSError("transport forbidden")
        with patch("stripe_double.webhooks.http.client.HTTPConnection",side_effect=record):
            # Call the dispatcher directly so the negative transport observer
            # cannot intercept this test's own client connection.
            with self.assertRaises(SimulatorError):
                self.server._internal("/__simulator/v1/runs/"+self.run+"/actions",self.run,{
                    "action":"deliver_next_event","expected_state_version":self.store.run(self.run)["state_version"],
                    "callback":"https://api.stripe.com/forbidden"})
        self.assertEqual(attempts,[])

    def test_malformed_internal_types_receive_closed_errors(self):
        cases = [
            ("/__simulator/v1/runs",{"scenario_id":[]}),
            ("/__simulator/v1/runs/"+self.run+"/actions",{"action":{},"expected_state_version":1}),
            ("/__simulator/v1/components/exchange",{"client_secret":"x","account":"x","purpose":[]}),
        ]
        for path,body in cases:
            with self.subTest(path=path):
                try:
                    status,doc,_ = self.request("POST",path,body,json_body=True,
                        token=self.server.credentials.admin_token if "/runs" in path else None)
                except http.client.RemoteDisconnected:
                    self.fail("malformed JSON must receive a closed 400, not a disconnected handler")
                self.assertEqual(status,400,doc)

    def test_settings_stale_version_pii_and_explicit_expiry(self):
        account = self.account()
        secret = self.session(account,"account_settings")["client_secret"]
        cap = self.exchange(secret,account,"account_settings")[1]
        result = self.component(cap["control_capability"],account,"account_settings","business_complete",cap["provider_version"])
        self.assertEqual(result[0],200,result[1])
        self.assertEqual(self.component(cap["control_capability"],account,"account_settings","bank_complete",cap["provider_version"])[0],409)
        status,doc,_ = self.request("POST","/__simulator/v1/components/actions",{
            "control_capability":cap["control_capability"],"account":account,"purpose":"account_settings",
            "action":"bank_complete","expected_provider_version":result[1]["provider_version"],"bank_account":"forbidden"},json_body=True)
        self.assertEqual(status,400,doc)
        self.assertEqual(self.admin("expire_sessions")[0],200)
        self.assertEqual(self.component(cap["control_capability"],account,"account_settings","exit",result[1]["provider_version"])[0],403)

    def test_actual_http_account_and_three_session_profiles(self):
        account = self.account()
        for purpose in FIXTURE["session_forms"]:
            with self.subTest(purpose=purpose):
                session = self.session(account,purpose)
                self.assertEqual(session["object"],"account_session")
                self.assertEqual(session["account"],account)
                self.assertFalse(session["livemode"])
                self.assertEqual(session["components"],FIXTURE["session_components"][purpose])
                self.assertEqual(session["expires_at"],int(self.clock())+300)
                self.assertNotEqual(self.session(account,purpose)["client_secret"],session["client_secret"])
        status,doc,head=self.request("GET","/v1/accounts/"+account)
        self.assertEqual(status,200,doc)
        self.assertEqual(doc["controller"],FIXTURE["controller"])
        self.assertEqual(head["X-Local-Simulator-Instance"],self.store.instance_id)
        self.assertEqual(head["X-Local-Simulator-Run"],self.run)
        self.assertEqual(head["Cache-Control"],"no-store")

    def test_capability_account_purpose_run_expiry_replay_and_restart_fences(self):
        account=self.account()
        other=self.account("other")
        secret=self.session(account)["client_secret"]
        self.assertEqual(self.exchange(secret,other)[0],403)
        self.assertEqual(self.exchange(secret,account,"payout_settings")[0],403)
        other_run=self.store.create_run("STRIPE-LOCAL-02")["simulation_run_id"]
        self.assertEqual(self.exchange(secret,account,run=other_run)[0],403)
        status,cap,_=self.exchange(secret,account)
        self.assertEqual(status,200,cap)
        self.assertEqual(self.exchange(secret,account)[0],403)
        self.assertEqual(self.component(cap["control_capability"],other,"onboarding","requirements_complete",cap["provider_version"])[0],403)
        self.assertEqual(self.component(cap["control_capability"],account,"onboarding","confirm",cap["provider_version"])[0],400)
        self.clock.advance(301)
        self.assertEqual(self.component(cap["control_capability"],account,"onboarding","requirements_complete",cap["provider_version"])[0],403)
        fresh=self.session(account)["client_secret"]
        old_admin=self.server.credentials.admin_token
        self.restart()
        self.assertEqual(self.exchange(fresh,account)[0],403)
        self.assertEqual(self.request("POST","/__simulator/v1/runs",{"scenario_id":"STRIPE-LOCAL-01"},token=old_admin,json_body=True)[0],403)
        self.assertEqual(self.exchange(self.session(account)["client_secret"],account)[0],200)

    def test_reset_invalidates_controls_retains_original_resources(self):
        account=self.account()
        status,cap,_=self.exchange(self.session(account)["client_secret"],account)
        self.assertEqual(status,200,cap)
        status,new,_=self.request("POST","/__simulator/v1/runs/"+self.run+"/reset",
            {"expected_state_version":self.store.run(self.run)["state_version"]},
            token=self.server.credentials.admin_token,json_body=True)
        self.assertEqual(status,200,new)
        self.assertNotEqual(new["simulation_run_id"],self.run)
        self.assertEqual(self.component(cap["control_capability"],account,"onboarding","requirements_complete",cap["provider_version"])[0],403)
        self.assertEqual(len(self.store.objects(self.run,"account")),1)

    def test_drop_account_commit_then_restart_replay_and_raw_drift(self):
        self.assertEqual(self.admin("drop_next_reply_after_commit")[0],200)
        with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):
            self.request("POST","/v1/accounts",FIXTURE["account_form"],key="lost")
        self.assertEqual(len(self.store.objects(self.run,"account")),1)
        self.restart()
        status,account,_=self.request("POST","/v1/accounts",FIXTURE["account_form"],key="lost")
        self.assertEqual(status,200,account)
        self.assertEqual(len(self.store.objects(self.run,"account")),1)
        changed=raw_form(FIXTURE["account_form"]).replace(b"country=US",b"country=%55S")
        self.assertEqual(self.request("POST","/v1/accounts",key="lost",raw=changed)[0],409)
        self.assertEqual(self.request("POST","/v1/accounts",FIXTURE["account_form"],key="lost",headers={"Stripe-Version":"bad"})[0],409)

    def test_payment_and_refund_drop_replay_preserves_resource(self):
        account=self.account()
        self.ready()
        self.assertEqual(self.admin("drop_next_reply_after_commit")[0],200)
        with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):
            self.request("POST","/v1/payment_intents",FIXTURE["intent_form"],account=account,key="lost-pi")
        self.restart()
        status,pi,_=self.request("POST","/v1/payment_intents",FIXTURE["intent_form"],account=account,key="lost-pi")
        self.assertEqual(status,200,pi)
        self.assertEqual(len(self.store.objects(self.run,"payment_intent")),1)
        ui=self.request("GET","/v1/payment_intents/"+pi["id"],account=account)[1]
        cap=self.exchange(ui["client_secret"],account,"card_payment")[1]
        self.assertEqual(self.component(cap["control_capability"],account,"card_payment","confirm",cap["provider_version"])[0],200)
        captured=self.request("POST","/v1/payment_intents/"+pi["id"]+"/capture",{"amount_to_capture":"1000"},account=account,key="capture")
        self.assertEqual(captured[0],200,captured[1])
        charge=captured[1]["latest_charge"]["id"]
        self.assertEqual(self.admin("refund_pending")[0],200)
        self.assertEqual(self.admin("drop_next_reply_after_commit")[0],200)
        values={"charge":charge,"amount":"600","metadata[refund_id]":"original","refund_application_fee":"false"}
        with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):
            self.request("POST","/v1/refunds",values,account=account,key="lost-refund")
        self.restart()
        status,refund,_=self.request("POST","/v1/refunds",values,account=account,key="lost-refund")
        self.assertEqual(status,200,refund)
        self.assertEqual(len(self.store.objects(self.run,"refund")),1)
        self.assertEqual(refund["status"],"pending")
        self.assertEqual(self.admin("refund_complete")[0],200)
        status,done,_=self.request("GET","/v1/refunds/"+refund["id"]+"?expand%5B%5D=balance_transaction",account=account)
        self.assertEqual(status,200,done)
        self.assertEqual(done["status"],"succeeded")
        self.assertEqual(done["amount"],600)
        self.assertEqual(done["charge"],charge)
        self.assertEqual(self.request("POST","/v1/refunds",{**values,"amount":"401"},account=account,key="over")[0],409)
        self.assertEqual(self.request("GET","/v1/refunds/"+refund["id"],account=self.account("wrong-account"))[0],403)

    def test_strict_transport_headers_json_forms_queries_and_bounds(self):
        account=self.account()
        rejects=[
            ("POST","/v1/accounts",raw_form(FIXTURE["account_form"])+b"&country=US",{}),
            ("POST","/v1/accounts",b"country=%ZZ",{}),
            ("POST","/v1/accounts",b"x=" + b"a"*MAX_BODY,{}),
            ("POST","/v1/accounts",b"{}",{"Content-Type":"application/json"}),
            ("GET","/v1/accounts/"+account+"?x=1",b"",{}),
            ("GET","/v1/accounts/"+account,b"x",{}),
            ("GET","/v1/accounts/"+account,b"",{"Host":"attacker.invalid"}),
            ("GET","http://127.0.0.1/v1/accounts/"+account,b"",{}),
            ("POST","/v1/account_sessions",raw_form({"account":account,**FIXTURE["session_forms"]["payout_settings"],"components[payouts][features][instant_payouts]":"true"}),{}),
        ]
        for method,path,body,headers in rejects:
            with self.subTest(path=path,headers=headers,body_length=len(body)):
                status,doc,_=self.request(method,path,raw=body,key="rejected",headers=headers)
                self.assertIn(status,(400,413),doc)
        for path in ["/v1/account_links","/v1/transfers","/v1/setup_intents","/v1/accounts/"+account+"/"]:
            status,doc,_=self.request("POST",path,key="unsupported")
            self.assertEqual(status,400,doc)
            self.assertEqual(doc["error"]["code"],"simulator_unsupported")
        self.assertEqual(self.request("GET","/v1/accounts/"+account,headers={"X-Local-Simulator-Run":"bad"})[0],403)
        self.assertEqual(self.request("GET","/v1/accounts/"+account,headers={"X-Local-Simulator-Instance":"bad"})[0],403)
        self.assertEqual(self.request("GET","/v1/accounts/"+account,token="sk_test_real")[0],403)
        self.assertEqual(self.request("POST","/__simulator/v1/runs",raw=b'{"scenario_id":"STRIPE-LOCAL-01","scenario_id":"STRIPE-LOCAL-02"}',token=self.server.credentials.admin_token,json_body=True)[0],400)

    def test_no_admin_or_capability_confusion_no_exit_capture(self):
        account,pi=self.payment()
        ui=self.request("GET","/v1/payment_intents/"+pi,account=account)[1]
        cap=self.exchange(ui["client_secret"],account,"card_payment")[1]
        self.assertEqual(self.request("POST","/__simulator/v1/runs",{"scenario_id":"STRIPE-LOCAL-01"},json_body=True)[0],403)
        self.assertEqual(self.request("GET","/v1/accounts/"+account,token=self.server.credentials.admin_token)[0],403)
        self.assertEqual(self.component(self.server.credentials.admin_token,account,"card_payment","confirm",cap["provider_version"])[0],403)
        before=self.store.object(self.run,"payment_intent",pi,account)
        self.assertEqual(self.component(cap["control_capability"],account,"card_payment","exit",cap["provider_version"])[0],200)
        after=self.store.object(self.run,"payment_intent",pi,account)
        self.assertEqual(before,after)
        self.assertEqual(after["status"],"requires_capture")
        self.assertIsNone(after["latest_charge"])

    def test_no_secrets_persist_in_sqlite_or_diagnostic_repr(self):
        account=self.account()
        session=self.session(account)
        cap=self.exchange(session["client_secret"],account)[1]
        for secret in [session["client_secret"],cap["control_capability"],self.server.credentials.api_key,self.server.credentials.admin_token]:
            self.assertNotIn(secret,repr(self.server))
            self.assertNotIn(secret,repr(self.server.credentials))
            self.assertNotIn(secret.encode(),self.path.read_bytes())

    def test_duplicate_headers_and_chunked_body_are_refused(self):
        for extra in [b"Content-Length: 0\r\nContent-Length: 0\r\n",b"Transfer-Encoding: chunked\r\n",b"Authorization: Bearer evil\r\nAuthorization: Bearer evil\r\n"]:
            sock=socket.create_connection(self.server.address,timeout=2)
            try:
                host=f"{self.server.address[0]}:{self.server.address[1]}"
                sock.sendall(b"POST /v1/accounts HTTP/1.1\r\nHost: "+host.encode()+b"\r\n"+extra+b"Connection: close\r\n\r\n")
                result=sock.recv(8192)
                self.assertIn(b" 400 ",result.split(b"\r\n",1)[0])
            finally:
                sock.close()

    def test_shutdown_closes_pending_connections_and_joins_all_workers(self):
        sock=socket.create_connection(self.server.address,timeout=2)
        sock.sendall(b"POST /v1/accounts HTTP/1.1\r\nHost:")
        deadline=time.monotonic()+1
        while self.server.active_connections==0 and time.monotonic()<deadline:
            time.sleep(.005)
        start=time.monotonic()
        self.server.close()
        self.assertLess(time.monotonic()-start,2)
        self.assertFalse(self.server.thread.is_alive())
        self.assertEqual(self.server.active_connections,0)
        try:
            self.assertEqual(sock.recv(1),b"")
        finally:
            sock.close()
        self.server.close()

    def test_nonlocal_bind_callback_and_synthetic_keys_refuse_before_transport(self):
        attempts=[]
        for callback in ["https://api.stripe.com/x","http://localhost:1234/x","http://127.0.0.1.evil:1234/x","http://127.0.0.1:1234/x?x=1","http://u:p@127.0.0.1:1234/x","http://127.0.0.1:1234/x#f"]:
            with self.subTest(callback=callback),patch("http.client.HTTPConnection",side_effect=lambda *a,**k:attempts.append(a)):
                with self.assertRaises((SimulatorError,ValueError)):
                    SimulatorServer(self.store,callback=callback)
        self.assertEqual(attempts,[])
        with self.assertRaises((SimulatorError,ValueError)):
            SimulatorServer(self.store,host="0.0.0.0")
        with self.assertRaises((SimulatorError,ValueError)):
            Credentials(api_key="sk_test_real",public_key="pk_test_real",admin_token="x",webhook_secret="whsec_real")


class CliTests(unittest.TestCase):
    script = Path(__file__).resolve().parents[1]

    def launch(self,path,*extra):
        read_fd,write_fd = os.pipe()
        process = None
        try:
            process = subprocess.Popen([sys.executable,"-I","-S","-B",str(self.script),"--state",str(path),
                "--bootstrap-fd",str(write_fd),*extra],pass_fds=(write_fd,),stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=path.parent)
            os.close(write_fd);write_fd = None
            self.assertTrue(select.select([read_fd],[],[],4)[0],"bootstrap timeout")
            data = os.read(read_fd,4096)
            if not data:
                out,err = process.communicate(timeout=4)
                self.fail("CLI bootstrap unavailable: " + err.decode())
            return process,json.loads(data)
        except BaseException:
            if process is not None and process.poll() is None:
                process.terminate();process.communicate(timeout=4)
            raise
        finally:
            os.close(read_fd)
            if write_fd is not None: os.close(write_fd)

    def stop(self,process):
        if process.poll() is None: process.terminate()
        out,err = process.communicate(timeout=4)
        self.assertEqual(process.returncode,0,err.decode())
        self.assertEqual(out,b"")
        self.assertEqual(err,b"")

    def test_cli_private_bootstrap_restart_and_pending_connection_shutdown(self):
        from urllib.parse import urlsplit
        path = case_path(self)
        process,first = self.launch(path,"--create","--scenario","STRIPE-LOCAL-01")
        sock = None
        try:
            origin = urlsplit(first["origin"])
            sock = socket.create_connection((origin.hostname,origin.port),timeout=2)
            sock.sendall(b"POST /v1/accounts HTTP/1.1\r\nHost:")
            started = time.monotonic()
            self.stop(process)
            self.assertLess(time.monotonic()-started,3)
        finally:
            if sock is not None: sock.close()
            if process.poll() is None: self.stop(process)
        process,second = self.launch(path,"--run",first["simulation_run_id"],"--instance",first["instance_id"])
        try:
            for field in ["instance_id","simulation_run_id","platform_account"]:
                self.assertEqual(first[field],second[field])
            self.assertTrue(first["admin_token"] != second["admin_token"])
            for candidate in path.parent.iterdir():
                if candidate.is_file():
                    data = candidate.read_bytes()
                    for field in ["api_key","admin_token","webhook_secret"]:
                        self.assertTrue(first[field].encode() not in data,"bootstrap secret persisted")
        finally:
            self.stop(process)

    def test_cli_refuses_regular_file_bootstrap_and_missing_resume(self):
        path = case_path(self)
        destination = path.parent / "forbidden-bootstrap"
        with destination.open("wb") as output:
            result = subprocess.run([sys.executable,"-I","-S","-B",str(self.script),"--state",str(path),
                "--create","--scenario","STRIPE-LOCAL-01","--bootstrap-fd",str(output.fileno())],
                pass_fds=(output.fileno(),),capture_output=True,timeout=4)
        self.assertEqual(result.returncode,1)
        self.assertFalse(path.exists())
        self.assertEqual(destination.read_bytes(),b"")
        self.assertEqual(result.stdout,b"")
        read_fd,write_fd = os.pipe()
        try:
            result = subprocess.run([sys.executable,"-I","-S","-B",str(self.script),"--state",str(path),
                "--run","missing","--instance","missing","--bootstrap-fd",str(write_fd)],
                pass_fds=(write_fd,),capture_output=True,timeout=4)
        finally:
            os.close(read_fd);os.close(write_fd)
        self.assertEqual(result.returncode,1)
        self.assertFalse(path.exists())


class WebhookTests(unittest.TestCase):
    setUp = HttpTests.setUp
    close = HttpTests.close
    request = HttpTests.request
    admin = HttpTests.admin
    account = HttpTests.account
    def test_shutdown_joins_handler_with_webhook_delivery_pending(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        entered,stop = threading.Event(),threading.Event()
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                inner.rfile.read(int(inner.headers["Content-Length"]))
                inner.send_response(200);inner.send_header("Content-Length","100");inner.end_headers()
                entered.set()
                try:
                    while not stop.wait(.15):
                        inner.wfile.write(b"x");inner.wfile.flush()
                except OSError: pass
            def log_message(self,*args): pass
        receiver = ThreadingHTTPServer(("127.0.0.1",0),Receiver)
        receiver.daemon_threads = False
        owner = threading.Thread(target=receiver.serve_forever,kwargs={"poll_interval":.02});owner.start()
        finished,errors = [],[]
        worker = None
        try:
            self.server.close()
            self.server = SimulatorServer(self.store,clock=self.clock,callback=f"http://127.0.0.1:{receiver.server_port}/hook").start()
            self.account()
            def deliver():
                try: finished.append(self.admin("deliver_next_event")[0])
                except (http.client.RemoteDisconnected,ConnectionResetError): finished.append("closed")
                except BaseException as error: errors.append(type(error).__name__)
            worker = threading.Thread(target=deliver);worker.start()
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            self.server.close()
            worker.join(3)
            self.assertLess(time.monotonic()-start,2.8)
            self.assertEqual(errors,[])
            self.assertEqual(len(finished),1)
            self.assertFalse(worker.is_alive())
            self.assertFalse(self.server.thread.is_alive())
            self.assertEqual(self.server.active_connections,0)
            self.assertEqual(self.store.events(self.run)[0]["delivery_state"],"failed")
        finally:
            stop.set();receiver.shutdown();receiver.server_close();owner.join(2)
            if worker is not None: worker.join(3)

    def test_slow_webhook_has_absolute_deadline(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        entered = threading.Event()
        stop = threading.Event()
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                inner.rfile.read(int(inner.headers["Content-Length"]))
                inner.send_response(200)
                inner.send_header("Content-Length","100")
                inner.end_headers()
                entered.set()
                try:
                    for _ in range(16):
                        if stop.wait(.2): break
                        inner.wfile.write(b"x")
                        inner.wfile.flush()
                except OSError:
                    pass
            def log_message(self,*args): pass
        receiver = ThreadingHTTPServer(("127.0.0.1",0),Receiver)
        receiver.daemon_threads = False
        owner = threading.Thread(target=receiver.serve_forever,kwargs={"poll_interval":.02})
        owner.start()
        result = []
        pump = None
        try:
            self.server.close()
            self.server = SimulatorServer(self.store,clock=self.clock,callback=f"http://127.0.0.1:{receiver.server_port}/hook").start()
            self.account()
            def deliver():
                started = time.monotonic()
                result.append((self.server.outbox.pump_once(self.run),time.monotonic()-started))
            pump = threading.Thread(target=deliver)
            pump.start()
            self.assertTrue(entered.wait(1))
            pump.join(4)
            self.assertFalse(pump.is_alive())
            self.assertLess(result[0][1],2.8,"trickle bytes must not extend the whole-delivery deadline")
            self.assertFalse(result[0][0]["delivered"])
        finally:
            stop.set()
            receiver.shutdown();receiver.server_close();owner.join(2)
            if pump is not None: pump.join(2)

    def test_redirect_is_not_followed_and_attempts_are_observable(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        received = []
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                inner.rfile.read(int(inner.headers["Content-Length"]))
                received.append(inner.path)
                inner.send_response(302)
                inner.send_header("Location","https://api.stripe.com/forbidden")
                inner.send_header("Content-Length","0")
                inner.end_headers()
            def log_message(self,*args): pass
        receiver = ThreadingHTTPServer(("127.0.0.1",0),Receiver)
        receiver.daemon_threads = False
        owner = threading.Thread(target=receiver.serve_forever,kwargs={"poll_interval":.02})
        owner.start()
        attempts = []
        connect = socket.socket.connect
        def recorded(sock,address):
            attempts.append(address)
            return connect(sock,address)
        try:
            self.server.close()
            self.server = SimulatorServer(self.store,clock=self.clock,callback=f"http://127.0.0.1:{receiver.server_port}/hook").start()
            self.account()
            with patch("socket.socket.connect",recorded):
                result = self.server.outbox.pump_once(self.run)
            self.assertFalse(result["delivered"])
            self.assertEqual(attempts,[("127.0.0.1",receiver.server_port)])
            self.assertEqual(received,["/hook"])
            self.assertEqual(self.server.outbox.attempts,1)
            self.assertEqual(self.store.events(self.run)[0]["delivery_state"],"failed")
        finally:
            receiver.shutdown();receiver.server_close();owner.join(2)

    def test_pinned_explicit_webhook_pump_signature_duplicate_and_no_redirect(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        received=[]
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                data=inner.rfile.read(int(inner.headers["Content-Length"]))
                received.append((data,inner.headers.get("Stripe-Signature")))
                inner.send_response(204)
                inner.end_headers()
            def log_message(self,*args):
                pass
        receiver=ThreadingHTTPServer(("127.0.0.1",0),Receiver)
        thread=threading.Thread(target=receiver.serve_forever)
        thread.start()
        self.addCleanup(lambda:(receiver.shutdown(),receiver.server_close(),thread.join(2)))
        self.server.close()
        self.server=SimulatorServer(self.store,clock=self.clock,callback=f"http://127.0.0.1:{receiver.server_port}/webhook")
        self.server.start()
        self.account()
        self.assertEqual(received,[])
        self.assertEqual(self.admin("deliver_next_event")[0],200)
        self.assertEqual(len(received),1)
        payload,signature=received[0]
        values=dict(x.split("=",1) for x in signature.split(","))
        expected=hmac.new(self.server.credentials.webhook_secret.encode(),values["t"].encode()+b"."+payload,hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(values["v1"],expected))
        self.assertEqual(self.admin("duplicate_last_event")[0],200)
        self.clock.advance(1)
        self.assertEqual(self.admin("deliver_next_event")[0],200)
        self.assertEqual(received[1][0],payload)
        self.assertNotEqual(received[1][1],signature)
        self.assertEqual(self.server.outbox.attempts,2)


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.case=HttpTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_delivery_validation_and_commit_failure_release_slot(self):
        c = self.case
        received,attempts = [],[]
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                received.append(inner.rfile.read(int(inner.headers["Content-Length"])))
                inner.send_response(204);inner.end_headers()
            def log_message(self,*args): pass
        receiver = ThreadingHTTPServer(("127.0.0.1",0),Receiver)
        receiver.daemon_threads = False
        owner = threading.Thread(target=receiver.serve_forever,kwargs={"poll_interval":.02});owner.start()
        connect = socket.socket.connect
        def observed(sock,address):
            attempts.append(address)
            return connect(sock,address)
        try:
            c.server.close()
            c.server = SimulatorServer(c.store,clock=c.clock,callback=f"http://127.0.0.1:{receiver.server_port}/hook").start()
            c.account()
            version = c.store.run(c.run)["state_version"]
            body = {"action":"deliver_next_event","expected_state_version":version}
            def post(value):
                return c.request("POST","/__simulator/v1/runs/"+c.run+"/actions",value,
                    token=c.server.credentials.admin_token,json_body=True)
            with patch("socket.socket.connect",observed):
                for value,status in [({**body,"expected_state_version":version-1},409),
                                     ({**body,"callback":"http://127.0.0.1:1/forbidden"},400),
                                     ({**body,"expected_state_version":"bad"},400)]:
                    self.assertEqual(post(value)[0],status)
                    self.assertEqual(c.store.run(c.run)["state_version"],version)
                c.store.connection.execute("CREATE TEMP TRIGGER reject_delivery BEFORE UPDATE OF state_version ON scenario_runs BEGIN SELECT RAISE(ABORT,'injected'); END")
                try:
                    with self.assertRaises(http.client.RemoteDisconnected): post(body)
                finally:
                    c.store.connection.execute("DROP TRIGGER reject_delivery")
                self.assertEqual(c.store.run(c.run)["state_version"],version)
                self.assertEqual(received,[])
                self.assertNotIn(("127.0.0.1",receiver.server_port),attempts)
                self.assertEqual(c.server.outbox.attempts,0)
                status,ack,_ = post(body)
                self.assertEqual(status,200,ack)
                self.assertTrue(ack["delivery"]["delivered"])
                self.assertEqual(ack["state_version"],version+1)
                self.assertEqual(attempts.count(("127.0.0.1",receiver.server_port)),1)
                self.assertEqual(post({"action":"expire_sessions","expected_state_version":ack["state_version"]})[0],200)
            self.assertEqual(len(received),1)
        finally:
            receiver.shutdown();receiver.server_close();owner.join(2)
            self.assertFalse(owner.is_alive())

    def test_transport_refusal_returns_committed_ack_without_retry_or_redirect(self):
        c = self.case
        received,attempts = [],[]
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                received.append(inner.rfile.read(int(inner.headers["Content-Length"])))
                inner.send_response(302)
                inner.send_header("Location","https://api.stripe.com/forbidden")
                inner.send_header("Content-Length","0");inner.end_headers()
            def log_message(self,*args): pass
        receiver = ThreadingHTTPServer(("127.0.0.1",0),Receiver)
        receiver.daemon_threads = False
        owner = threading.Thread(target=receiver.serve_forever,kwargs={"poll_interval":.02});owner.start()
        connect = socket.socket.connect
        def observed(sock,address):
            attempts.append(address)
            return connect(sock,address)
        try:
            c.server.close()
            c.server = SimulatorServer(c.store,clock=c.clock,callback=f"http://127.0.0.1:{receiver.server_port}/hook").start()
            c.account()
            version = c.store.run(c.run)["state_version"]
            with patch("socket.socket.connect",observed):
                status,ack,_ = c.admin("deliver_next_event")
                self.assertEqual(status,200,ack)
                self.assertEqual(ack["state_version"],version+1)
                self.assertFalse(ack["delivery"]["delivered"])
                self.assertEqual(c.request("POST","/__simulator/v1/runs/"+c.run+"/actions",
                    {"action":"expire_sessions","expected_state_version":ack["state_version"]},
                    token=c.server.credentials.admin_token,json_body=True)[0],200)
            self.assertEqual(len(received),1)
            self.assertEqual(attempts.count(("127.0.0.1",receiver.server_port)),1)
            self.assertTrue(all(address in [c.server.address,("127.0.0.1",receiver.server_port)] for address in attempts))
            self.assertEqual(c.server.outbox.attempts,1)
            self.assertEqual(c.store.events(c.run)[0]["delivery_state"],"failed")
        finally:
            receiver.shutdown();receiver.server_close();owner.join(2)
            self.assertFalse(owner.is_alive())

    def test_original_create_key_survives_in_recoverable_webhook(self):
        c=self.case
        account=c.account()
        c.ready()
        original='review-original-create-key'
        self.assertEqual(c.admin('drop_next_reply_after_commit')[0],200)
        with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):
            c.request('POST','/v1/payment_intents',FIXTURE['intent_form'],account=account,key=original)
        c.restart()
        events=[json.loads(bytes(row['payload'])) for row in c.store.events(c.run)]
        event=next(e for e in events if e['type']=='payment_intent.created')
        self.assertEqual(event['request']['idempotency_key'],original,
            'durable create webhook loses the original request key needed for unknown-intent recovery')

    def test_callback_refusal_does_not_silently_advance_version(self):
        c=self.case
        before=c.store.run(c.run)['state_version']
        status,body,_=c.request('POST','/__simulator/v1/runs/'+c.run+'/actions',
            {'action':'deliver_next_event','expected_state_version':before},
            token=c.server.credentials.admin_token,json_body=True)
        after=c.store.run(c.run)['state_version']
        follow,follow_body,_=c.request('POST','/__simulator/v1/runs/'+c.run+'/actions',
            {'action':'requirements_complete','expected_state_version':before},
            token=c.server.credentials.admin_token,json_body=True)
        self.assertEqual(status,409)
        self.assertEqual(after,before,'refused delivery commits an undisclosed run version')
        self.assertEqual(follow,200)

    def test_busy_delivery_does_not_invalidate_last_successful_version(self):
        c=self.case
        entered,release=threading.Event(),threading.Event()
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(inner):
                inner.rfile.read(int(inner.headers['Content-Length']))
                entered.set()
                release.wait(1.5)
                try:
                    inner.send_response(204);inner.end_headers()
                except OSError: pass
            def log_message(self,*args): pass
        receiver=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        receiver.daemon_threads=False
        owner=threading.Thread(target=receiver.serve_forever,kwargs={'poll_interval':.02})
        owner.start()
        worker=None; results=[];errors=[]
        try:
            c.server.close()
            c.server=SimulatorServer(c.store,clock=c.clock,callback=f'http://127.0.0.1:{receiver.server_port}/hook').start()
            c.account()
            initial=c.store.run(c.run)['state_version']
            def post(action,version):
                return c.request('POST','/__simulator/v1/runs/'+c.run+'/actions',
                    {'action':action,'expected_state_version':version},
                    token=c.server.credentials.admin_token,json_body=True)
            def first():
                try: results.append(post('deliver_next_event',initial))
                except BaseException as error: errors.append(type(error).__name__)
            worker=threading.Thread(target=first);worker.start()
            self.assertTrue(entered.wait(1))
            # Obtain an actual successful HTTP acknowledgement while delivery is pending.
            ok,current,_=post('requirements_complete',initial+1)
            self.assertEqual(ok,200,current)
            status,busy,_=post('deliver_next_event',current['state_version'])
            release.set();worker.join(3)
            self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
            follow,body,_=post('expire_sessions',current['state_version'])
            after=c.store.run(c.run)['state_version']
            self.assertEqual(status,409)
            self.assertEqual(busy['error']['code'],'simulator_delivery_busy')
            self.assertEqual(follow,200,'busy delivery invalidates the last successfully acknowledged version')
        finally:
            release.set()
            if worker is not None: worker.join(3)
            receiver.shutdown();receiver.server_close();owner.join(2)
            self.assertFalse(owner.is_alive())


if __name__ == "__main__":
    unittest.main()
