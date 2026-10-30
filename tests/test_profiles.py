"""API-version isolation, current forms and original Acacia state compatibility."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from stripe_double.state import StateStore
from stripe_double.wire import API_VERSION, LEGACY_API_VERSION, SimulatorError
import test_http
from test_state import FIXTURE, case_path, raw_form

ROOT = Path(__file__).resolve().parents[1]


class ProfileStateTests(unittest.TestCase):
    def store(self, path, **kwargs):
        store = StateStore(path, **kwargs)
        self.addCleanup(store.close)
        return store

    def account(self, store, run, key="profile-account", version=None):
        return store.request(run, None, "POST", "/v1/accounts", raw_form(FIXTURE["account_form"]),
            store.api_version if version is None else version, key)

    def test_api_version_persists_and_cannot_change_on_restart_or_replay(self):
        path = case_path(self)
        store = self.store(path, create=True)
        run = store.create_run("STRIPE-LOCAL-01")["simulation_run_id"]
        account = self.account(store, run)
        self.assertEqual(account.status, 200)
        self.assertEqual(store.api_version, "2026-10-28.endive")
        store.close()
        with self.assertRaises(SimulatorError) as refused:
            self.store(path, api_version=LEGACY_API_VERSION)
        self.assertEqual(refused.exception.code, "simulator_api_version")
        store = self.store(path)
        self.assertEqual(store.api_version, API_VERSION)
        self.assertEqual(self.account(store, run).body, account.body)
        self.assertEqual(self.account(store, run, version=LEGACY_API_VERSION).status, 409)
        self.assertEqual(len(store.objects(run, "account")), 1)
        self.assertEqual(json.loads(bytes(store.events(run)[0]["payload"]))["api_version"], API_VERSION)
        self.assertEqual(store.request(run, None, "GET", "/v1/accounts/" + account.body["id"],
            api_version=None).status, 400)

    def test_explicit_acacia_retains_original_form_and_event_version(self):
        path = case_path(self)
        store = self.store(path, create=True, api_version=LEGACY_API_VERSION)
        run = store.create_run("STRIPE-LOCAL-06")["simulation_run_id"]
        account = self.account(store, run).body["id"]
        store.scenario_action(run, "requirements_complete", store.run(run)["state_version"])
        form = dict(FIXTURE["intent_form"])
        self.assertEqual(store.request(run, account, "POST", "/v1/payment_intents", raw_form(form),
            key="wrong-shape").status, 400)
        form["payment_method_types[]"] = form.pop("allowed_payment_method_types[]")
        intent = store.request(run, account, "POST", "/v1/payment_intents", raw_form(form), key="intent")
        self.assertEqual(intent.status, 200, intent.body)
        self.assertEqual(intent.body["payment_method_types"], ["card"])
        events = [json.loads(bytes(row["payload"])) for row in store.events(run)]
        self.assertTrue(all(event["api_version"] == LEGACY_API_VERSION for event in events))
        store.close()
        store = self.store(path)
        self.assertEqual(store.api_version, LEGACY_API_VERSION)
        self.assertEqual(store.request(run, account, "POST", "/v1/payment_intents", raw_form(form),
            key="intent").body, intent.body)

    def test_acacia_multiple_valid_captures_do_not_hit_old_per_request_cap(self):
        store = self.store(case_path(self),create=True,api_version=LEGACY_API_VERSION)
        run = store.create_run("STRIPE-LOCAL-01")["simulation_run_id"]
        account = self.account(store,run).body['id']
        store.scenario_action(run,'requirements_complete',store.run(run)['state_version'])
        form = {'amount':'60000000','currency':'usd','capture_method':'manual','payment_method_types[]':'card'}
        for index in range(2):
            intent = store.request(run,account,'POST','/v1/payment_intents',raw_form(form),key=f'intent-{index}')
            self.assertEqual(intent.status,200,intent.body)
            confirmed = store.request(run,account,'POST','/v1/payment_intents/'+intent.body['id']+'/confirm',
                raw_form({'payment_method':'pm_card_visa'}),key=f'confirm-{index}')
            self.assertEqual(confirmed.status,200,confirmed.body)
            captured = store.request(run,account,'POST','/v1/payment_intents/'+intent.body['id']+'/capture',
                raw_form({'amount_to_capture':'60000000'}),key=f'capture-{index}')
            self.assertEqual(captured.status,200,captured.body)
            self.assertIsNone(captured.body['latest_charge']['balance_transaction'])

    def test_acacia_bookkeeping_does_not_consume_existing_resource_capacity(self):
        store = self.store(case_path(self),create=True,api_version=LEGACY_API_VERSION)
        run = store.create_run('STRIPE-LOCAL-01')['simulation_run_id']
        with patch('stripe_double.wire.MAX_OBJECTS',5):
            account = self.account(store,run).body['id']
            store.scenario_action(run,'requirements_complete',store.run(run)['state_version'])
            form = {'amount':'1000','currency':'usd','capture_method':'manual','payment_method_types[]':'card'}
            for index in range(2):
                intent = store.request(run,account,'POST','/v1/payment_intents',raw_form(form),key=f'cap-intent-{index}')
                self.assertEqual(intent.status,200,intent.body)
                result = store.request(run,account,'POST','/v1/payment_intents/'+intent.body['id']+'/confirm',
                    raw_form({'payment_method':'pm_card_visa'}),key=f'cap-confirm-{index}')
                self.assertEqual(result.status,200,result.body)
                result = store.request(run,account,'POST','/v1/payment_intents/'+intent.body['id']+'/capture',
                    raw_form({'amount_to_capture':'1000'}),key=f'cap-capture-{index}')
                self.assertEqual(result.status,200,result.body)

    def test_original_schema_reopens_as_acacia_without_rewriting_it(self):
        path = case_path(self)
        lock = path.with_name(path.name + ".lock")
        for file in (path, lock):
            descriptor = os.open(file, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(descriptor)
        instance = "11111111-1111-4111-8111-111111111111"
        platform = "acct_LocalSim" + instance.replace("-", "")
        connection = sqlite3.connect(path)
        try:
            connection.executescript((ROOT / "tests/fixtures/acacia_v1.sql").read_text())
            connection.execute("INSERT INTO simulator_instances VALUES(?,?,1)", (instance, platform))
            connection.commit()
        finally:
            connection.close()
        original = path.read_bytes()
        store = self.store(path)
        self.assertEqual(store.api_version, LEGACY_API_VERSION)
        self.assertEqual(store.instance_id, instance)
        store.close()
        self.assertEqual(path.read_bytes(), original)
        with self.assertRaises(SimulatorError):
            self.store(path, api_version=API_VERSION)
        self.assertEqual(path.read_bytes(), original)
        store = self.store(path)
        run = store.create_run("STRIPE-LOCAL-01")["simulation_run_id"]
        account = self.account(store, run)
        self.assertEqual(account.status, 200)
        payload = bytes(store.events(run)[0]["payload"])
        self.assertEqual(json.loads(payload)["api_version"], LEGACY_API_VERSION)
        store.close()
        store = self.store(path)
        self.assertEqual(self.account(store, run).body, account.body)
        self.assertEqual(bytes(store.events(run)[0]["payload"]), payload)

    def test_unknown_profile_refuses_before_allocating_state(self):
        path = case_path(self)
        for version in ("2026-10-28.preview", "latest", [], False):
            with self.subTest(version=version), self.assertRaises(SimulatorError):
                self.store(path, create=True, api_version=version)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_name(path.name + ".lock").exists())

    def test_unknown_persisted_profile_refuses_without_recreating_state(self):
        path = case_path(self)
        store = self.store(path, create=True)
        instance = store.instance_id
        store.connection.execute("UPDATE simulator_instances SET api_version='unknown'")
        store.close()
        original = path.read_bytes()
        with self.assertRaises(SimulatorError):
            self.store(path)
        self.assertEqual(path.read_bytes(), original)
        connection = sqlite3.connect(path)
        try:
            self.assertEqual(connection.execute("SELECT instance_id FROM simulator_instances").fetchone()[0], instance)
        finally:
            connection.close()


class ProfileCliTests(unittest.TestCase):
    script = ROOT
    launch = test_http.CliTests.launch
    stop = test_http.CliTests.stop

    def test_default_bootstrap_reports_current_profile_and_restart_retains_it(self):
        path = case_path(self)
        process, first = self.launch(path, "--create", "--scenario", "STRIPE-LOCAL-01")
        try:
            self.assertEqual(first["schema_version"], 2)
            self.assertEqual(first["api_version"], "2026-10-28.endive")
            self.assertEqual(first["protocol_profile"], "stripe_standard_us_usd_endive_v1")
        finally:
            self.stop(process)
        process, second = self.launch(path, "--run", first["simulation_run_id"],
            "--instance", first["instance_id"])
        try:
            for field in ("api_version", "protocol_profile", "instance_id", "simulation_run_id"):
                self.assertEqual(first[field], second[field])
        finally:
            self.stop(process)

    def test_explicit_legacy_bootstrap_preserves_the_strict_contract(self):
        path = case_path(self)
        process, first = self.launch(path, "--create", "--scenario", "STRIPE-LOCAL-01",
            "--api-version", LEGACY_API_VERSION)
        try:
            self.assertEqual(first["schema_version"], 1)
            self.assertEqual(first["protocol_profile"], "stripe_standard_us_usd_test_v1")
            self.assertEqual(set(first), {"schema_version", "instance_id", "simulation_run_id",
                "scenario_id", "platform_account", "protocol_profile", "origin", "api_key",
                "public_key", "admin_token", "webhook_secret"})
        finally:
            self.stop(process)
        read_fd, write_fd = os.pipe()
        try:
            refused = subprocess.run([sys.executable, "-I", "-S", "-B", str(ROOT),
                "--state", str(path), "--run", first["simulation_run_id"],
                "--instance", first["instance_id"], "--api-version", API_VERSION,
                "--bootstrap-fd", str(write_fd)], pass_fds=(write_fd,), capture_output=True, timeout=4)
            os.close(write_fd)
            write_fd = None
            self.assertEqual(refused.returncode, 1)
            self.assertEqual(refused.stdout, b"")
            self.assertEqual(os.read(read_fd, 4096), b"", "conflicting profile produced a bootstrap")
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
        process, second = self.launch(path, "--run", first["simulation_run_id"],
            "--instance", first["instance_id"])
        try:
            self.assertEqual(second["schema_version"], 1)
            self.assertEqual(second["protocol_profile"], first["protocol_profile"])
        finally:
            self.stop(process)


if __name__ == "__main__":
    unittest.main()
