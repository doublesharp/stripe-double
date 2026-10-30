"""Portable provider behavior. No application database or real provider access."""
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from stripe_double.state import StateStore
from stripe_double.wire import API_VERSION, SimulatorError

FIXTURE = json.loads((Path(__file__).parent / "fixtures/stripe_double_wire.json").read_text())


class Clock:
    def __init__(self):
        self.now = 1792900000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def case_path(test):
    directory = tempfile.TemporaryDirectory(prefix="stripe-double-case-")
    test.addCleanup(directory.cleanup)
    return Path(directory.name).resolve() / "provider.sqlite3"


def raw_form(values):
    return urlencode(values).encode("ascii")


class LockTests(unittest.TestCase):
    def test_owner_lock_allows_sqlite_create_and_restart(self):
        path = case_path(self)
        store = None
        try:
            try:
                store = StateStore(path, create=True)
                run = store.create_run("STRIPE-LOCAL-01")["simulation_run_id"]
                instance = store.instance_id
                store.close()
                store = StateStore(path)
                self.assertEqual(store.instance_id, instance)
                self.assertEqual(store.run(run)["simulation_run_id"], run)
            except sqlite3.OperationalError as error:
                self.fail("owner lock must coexist with SQLite: " + str(error))
        finally:
            if store is not None:
                store.close()


class StateTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.path = case_path(self)
        self.store = StateStore(self.path, create=True, clock=self.clock)
        self.addCleanup(lambda: self.store.close())
        self.run = self.store.create_run("STRIPE-LOCAL-01")["simulation_run_id"]

    def invoke(self, method, path, values=None, account=None, key=None, raw=None, run=None):
        return self.store.request(
            run or self.run, account, method, path,
            raw if raw is not None else raw_form(values or {}),
            API_VERSION, key,
        )

    def account(self, key="account-original"):
        result = self.invoke("POST", "/v1/accounts", FIXTURE["account_form"], key=key)
        self.assertEqual(result.status, 200, result.body)
        return result.body

    def ready(self):
        version = self.store.run(self.run)["state_version"]
        self.store.scenario_action(self.run, "requirements_complete", version)

    def captured(self):
        account = self.account()["id"]
        self.ready()
        pi = self.invoke("POST", "/v1/payment_intents", FIXTURE["intent_form"], account, "pi-original")
        self.assertEqual(pi.status, 200, pi.body)
        current = self.store.object(self.run, "payment_intent", pi.body["id"], account)
        self.store.component_action(self.run, account, "card_payment", current["id"], "confirm", current["version"])
        capture = self.invoke("POST", "/v1/payment_intents/" + current["id"] + "/capture", {"amount_to_capture":"1000"}, account, "capture-original")
        self.assertEqual(capture.status, 200, capture.body)
        return account, capture.body

    def test_all_api_mutation_events_preserve_keys_controls_stay_unkeyed(self):
        account,pi = self.captured()
        charge = pi["latest_charge"]
        refund = self.invoke("POST","/v1/refunds",{"charge":charge["id"],"amount":"100",
            "refund_application_fee":"false"},account,"refund-key")
        self.assertEqual(refund.status,200,refund.body)
        fee_refund = self.invoke("POST","/v1/application_fees/"+charge["application_fee"]+"/refunds",
            {"amount":"10"},key="fee-key")
        self.assertEqual(fee_refund.status,200,fee_refund.body)
        cancel_pi = self.invoke("POST","/v1/payment_intents",FIXTURE["intent_form"],account,"cancel-create-key")
        self.assertEqual(cancel_pi.status,200,cancel_pi.body)
        cancelled = self.invoke("POST","/v1/payment_intents/"+cancel_pi.body["id"]+"/cancel",{},account,"cancel-key")
        self.assertEqual(cancelled.status,200,cancelled.body)
        events = [json.loads(bytes(row["payload"])) for row in self.store.events(self.run)]
        observed = [(event["type"],event["request"]["idempotency_key"]) for event in events]
        self.assertEqual(observed,[
            ("account.updated","account-original"),("account.updated",None),
            ("payment_intent.created","pi-original"),("payment_intent.amount_capturable_updated",None),
            ("application_fee.created","capture-original"),("payment_intent.succeeded","capture-original"),
            ("refund.created","refund-key"),("application_fee.refunded","fee-key"),
            ("payment_intent.created","cancel-create-key"),("payment_intent.canceled","cancel-key")])
        original = bytes(self.store.events(self.run)[-1]["payload"])
        self.store.scenario_action(self.run,"duplicate_last_event",self.store.run(self.run)["state_version"])
        self.store.close()
        self.store = StateStore(self.path,clock=self.clock)
        self.assertEqual(bytes(self.store.events(self.run)[-1]["payload"]),original)
        count = len(self.store.events(self.run))
        self.assertEqual(self.invoke("POST","/v1/payment_intents/"+cancel_pi.body["id"]+"/cancel",{},account,"cancel-key").body,cancelled.body)
        self.assertEqual(len(self.store.events(self.run)),count)
        self.store.scenario_action(self.run,"requirements_required",self.store.run(self.run)["state_version"])
        self.assertIsNone(json.loads(bytes(self.store.events(self.run)[-1]["payload"]))["request"]["idempotency_key"])

    def test_original_key_changed_query_is_conflict(self):
        self.account()
        result = self.invoke("POST","/v1/accounts?unsupported=1",FIXTURE["account_form"],key="account-original")
        self.assertEqual(result.status,409)
        self.assertEqual(len(self.store.objects(self.run,"account")),1)

    def test_failed_idempotency_insert_rolls_back_resource_and_outbox(self):
        self.store.connection.execute("CREATE TEMP TRIGGER fail_idempotency BEFORE INSERT ON idempotency BEGIN SELECT RAISE(ABORT,'injected'); END")
        result = self.invoke("POST","/v1/accounts",FIXTURE["account_form"],key="atomic-idem")
        self.assertEqual(result.status,503)
        self.assertEqual(self.store.objects(self.run,"account"),[])
        self.assertEqual(self.store.events(self.run),[])
        self.store.connection.execute("DROP TRIGGER fail_idempotency")
        self.assertEqual(self.invoke("POST","/v1/accounts",FIXTURE["account_form"],key="atomic-idem").status,200)

    def test_process_owner_lock_and_missing_state_never_reconstruct(self):
        code = "from stripe_double.state import StateStore; import sys; s=StateStore(sys.argv[1]); s.close()"
        env = dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        child = subprocess.run([sys.executable,"-B","-c",code,str(self.path)],env=env,capture_output=True,timeout=3)
        self.assertNotEqual(child.returncode,0)
        self.store.close()
        child = subprocess.run([sys.executable,"-B","-c",code,str(self.path)],env=env,capture_output=True,timeout=3)
        self.assertEqual(child.returncode,0,child.stderr.decode())
        self.store = StateStore(self.path,clock=self.clock)
        self.path.unlink()
        self.assertEqual(self.invoke("POST","/v1/accounts",FIXTURE["account_form"],key="lost-file").status,503)
        self.assertFalse(self.path.exists())

    def test_original_key_changed_api_version_is_conflict(self):
        self.account()
        result = self.store.request(self.run,None,"POST","/v1/accounts",raw_form(FIXTURE["account_form"]),"other-version","account-original")
        self.assertEqual(result.status,409)

    def test_exact_adapter_capture_form_expands_original_charge(self):
        account = self.account()["id"]
        self.ready()
        pi = self.invoke("POST","/v1/payment_intents",FIXTURE["intent_form"],account,"pi").body
        current = self.store.object(self.run,"payment_intent",pi["id"],account)
        self.store.component_action(self.run,account,"card_payment",pi["id"],"confirm",current["version"])
        result = self.invoke("POST","/v1/payment_intents/"+pi["id"]+"/capture",
            FIXTURE["capture_form"],account,"adapter-capture")
        self.assertEqual(result.status,200,result.body)
        self.assertEqual(result.body["latest_charge"]["amount_captured"],1000)

    def test_outbox_failure_rolls_back_resource_and_idempotency(self):
        with patch.object(self.store,"_event",side_effect=sqlite3.OperationalError("injected write failure")):
            result = self.invoke("POST","/v1/accounts",FIXTURE["account_form"],key="atomic")
        self.assertEqual(result.status,503)
        self.assertEqual(self.store.objects(self.run,"account"),[])
        self.assertEqual(self.store.events(self.run),[])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM idempotency").fetchone()[0],0)
        self.assertEqual(self.invoke("POST","/v1/accounts",FIXTURE["account_form"],key="atomic").status,200)

    def test_capacity_failure_rolls_back_without_consuming_key(self):
        with patch("stripe_double.wire.MAX_EVENTS",0):
            result = self.invoke("POST","/v1/accounts",FIXTURE["account_form"],key="capacity")
        self.assertEqual(result.status,429)
        self.assertEqual(self.store.objects(self.run,"account"),[])
        self.assertEqual(self.invoke("POST","/v1/accounts",FIXTURE["account_form"],key="capacity").status,200)

    def test_decline_cancel_and_readiness_never_capture(self):
        account = self.account()["id"]
        self.assertEqual(self.invoke("POST","/v1/payment_intents",FIXTURE["intent_form"],account,"blocked").status,409)
        self.ready()
        pi = self.invoke("POST","/v1/payment_intents",FIXTURE["intent_form"],account,"pi").body
        self.store.scenario_action(self.run,"decline_next_confirmation",self.store.run(self.run)["state_version"])
        current = self.store.object(self.run,"payment_intent",pi["id"],account)
        self.store.component_action(self.run,account,"card_payment",pi["id"],"confirm",current["version"])
        self.assertEqual(self.invoke("POST","/v1/payment_intents/"+pi["id"]+"/capture",{"amount_to_capture":"1000"},account,"capture").status,409)
        canceled = self.invoke("POST","/v1/payment_intents/"+pi["id"]+"/cancel",{},account,"cancel")
        self.assertEqual(canceled.body["status"],"canceled")
        self.assertEqual(self.store.objects(self.run,"charge"),[])

    def test_account_replay_survives_restart_without_new_resource(self):
        first = self.account()
        self.store.close()
        self.store = StateStore(self.path, clock=self.clock)
        replay = self.account()
        self.assertEqual(first, replay)
        self.assertEqual(len(self.store.objects(self.run, "account")), 1)
        self.assertEqual(first["controller"], FIXTURE["controller"])
        self.assertFalse(first["charges_enabled"])
        self.assertEqual(first["metadata"], {"setup_id": FIXTURE["account_form"]["metadata[setup_id]"]})

    def test_same_key_changed_raw_bytes_or_version_is_conflict(self):
        original = raw_form(FIXTURE["account_form"])
        self.account()
        self.assertEqual(self.invoke("POST", "/v1/accounts", key="account-original", raw=original + b"&").status, 409)
        self.assertEqual(len(self.store.objects(self.run, "account")), 1)

    def test_creation_is_explicit_missing_and_corrupt_state_fail_closed(self):
        absent = self.path.parent / "absent.sqlite3"
        with self.assertRaises((SimulatorError, OSError)):
            StateStore(absent, clock=self.clock)
        self.assertFalse(absent.exists())
        with self.assertRaises((SimulatorError, OSError)):
            StateStore(self.path, create=True, clock=self.clock)
        corrupt = self.path.parent / "corrupt.sqlite3"
        corrupt.write_bytes(b"not sqlite")
        corrupt.chmod(0o600)
        corrupt.with_name(corrupt.name+".lock").touch(mode=0o600)
        with self.assertRaises((SimulatorError, sqlite3.Error)):
            StateStore(corrupt, clock=self.clock)
        self.assertEqual(corrupt.read_bytes(), b"not sqlite")

    def test_single_writer_lock_and_idempotent_close(self):
        with self.assertRaises((SimulatorError, OSError)):
            StateStore(self.path, clock=self.clock)
        self.store.close()
        self.store.close()
        self.store = StateStore(self.path, clock=self.clock)

    def test_reset_retains_tombstones_but_refuses_old_run_mutation(self):
        account = self.account()
        fresh = self.store.reset_run(self.run, self.store.run(self.run)["state_version"])
        self.assertNotEqual(fresh["simulation_run_id"], self.run)
        self.assertEqual(self.store.object(self.run, "account", account["id"], account["id"])["id"], account["id"])
        result = self.invoke("POST", "/v1/accounts", FIXTURE["account_form"], key="replacement")
        self.assertEqual(result.status, 409)
        self.assertEqual(len(self.store.objects(self.run, "account")), 1)

    def test_capture_pending_refund_completion_and_conservation(self):
        account, pi = self.captured()
        charge = pi["latest_charge"]
        self.assertTrue(charge["paid"])
        self.assertTrue(charge["captured"])
        self.assertEqual(charge["payment_method_details"]["type"], "card")
        self.assertTrue(charge["balance_transaction"].startswith("txn_LocalSim"))
        version = self.store.run(self.run)["state_version"]
        self.store.scenario_action(self.run, "refund_pending", version)
        values = {"charge":charge["id"], "amount":"600", "metadata[refund_id]":"refund-original", "refund_application_fee":"false"}
        refund = self.invoke("POST", "/v1/refunds", values, account, "refund-original")
        self.assertEqual(refund.status, 200, refund.body)
        self.assertEqual(refund.body["status"], "pending")
        self.clock.advance(600)
        pending = self.invoke("GET", "/v1/refunds/" + refund.body["id"], account=account)
        self.assertEqual(pending.body["status"], "pending", "wall clock must not advance refund")
        values["amount"] = "401"
        self.assertEqual(self.invoke("POST", "/v1/refunds", values, account, "too-much").status, 409)
        self.store.scenario_action(self.run, "refund_complete", self.store.run(self.run)["state_version"])
        done = self.invoke("GET", "/v1/refunds/" + refund.body["id"], account=account)
        self.assertEqual(done.body["status"], "succeeded")
        self.assertEqual(done.body["charge"], charge["id"])
        self.assertEqual(done.body["payment_intent"], pi["id"])
        fee = self.invoke("GET", "/v1/application_fees/" + charge["application_fee"])
        self.assertEqual(fee.status, 200, fee.body)
        returned = self.invoke("POST", "/v1/application_fees/" + fee.body["id"] + "/refunds", {"amount":"100","metadata[refund_id]":"fee-return"}, key="fee-return")
        self.assertEqual(returned.status, 200, returned.body)
        self.assertEqual(returned.body["fee"], fee.body["id"])
        again = self.invoke("POST", "/v1/application_fees/" + fee.body["id"] + "/refunds", {"amount":"1","metadata[refund_id]":"fee-over"}, key="fee-over")
        self.assertEqual(again.status, 409)

    def test_outbox_atomic_and_duplicate_bytes_are_identical(self):
        account = self.account()
        rows = self.store.events(self.run)
        self.assertGreater(len(rows), 0)
        event = rows[-1]
        payload = bytes(event["payload"])
        self.store.scenario_action(self.run, "duplicate_last_event", self.store.run(self.run)["state_version"])
        duplicate = self.store.events(self.run)[-1]
        self.assertEqual(bytes(duplicate["payload"]), payload)
        self.assertEqual(json.loads(payload)["account"], account["id"])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), duplicate["payload_sha256"])

    def test_exit_does_not_capture_or_add_domain_authority(self):
        account = self.account()["id"]
        self.ready()
        pi = self.invoke("POST", "/v1/payment_intents", FIXTURE["intent_form"], account, "pi").body
        current = self.store.object(self.run, "payment_intent", pi["id"], account)
        before = len(self.store.events(self.run))
        self.store.component_action(self.run, account, "card_payment", pi["id"], "exit", current["version"])
        after = self.store.object(self.run, "payment_intent", pi["id"], account)
        self.assertEqual(after["status"], "requires_payment_method")
        self.assertIsNone(after["latest_charge"])
        self.assertEqual(len(self.store.events(self.run)), before)
        tables = {x[0] for x in self.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(tables & {"orders","ledger","consents","memberships","entitlements"})

    def test_provider_state_contains_no_volatile_secrets(self):
        self.account()
        data = self.path.read_bytes()
        self.assertNotIn(b"client_secret", data)
        self.assertNotIn(b"admin_token", data)


if __name__ == "__main__":
    unittest.main()
