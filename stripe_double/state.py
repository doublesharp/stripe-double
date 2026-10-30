"""Durable synthetic provider resources. SQLite owns no application domain data."""
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
import calendar
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from urllib.parse import urlsplit
from . import scenarios, wire
from .wire import SimulatorError, Result, need

SCHEMA = """
CREATE TABLE simulator_instances(instance_id TEXT PRIMARY KEY,platform_account TEXT NOT NULL,schema_version INTEGER NOT NULL,api_version TEXT NOT NULL);
CREATE TABLE scenario_runs(simulation_run_id TEXT PRIMARY KEY,scenario_id TEXT NOT NULL,state_version INTEGER NOT NULL,retired INTEGER NOT NULL,state TEXT NOT NULL);
CREATE TABLE provider_objects(simulation_run_id TEXT NOT NULL,account_id TEXT NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,version INTEGER NOT NULL,document TEXT NOT NULL,PRIMARY KEY(simulation_run_id,kind,object_id),FOREIGN KEY(simulation_run_id) REFERENCES scenario_runs);
CREATE TABLE idempotency(simulation_run_id TEXT NOT NULL,account_id TEXT NOT NULL,method TEXT NOT NULL,path TEXT NOT NULL,key TEXT NOT NULL,request_sha256 TEXT NOT NULL,resource_id TEXT,status INTEGER NOT NULL,response_projection TEXT NOT NULL,PRIMARY KEY(simulation_run_id,account_id,method,path,key),FOREIGN KEY(simulation_run_id) REFERENCES scenario_runs);
CREATE TABLE webhook_outbox(delivery_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,simulation_run_id TEXT NOT NULL,payload BLOB NOT NULL,payload_sha256 TEXT NOT NULL,delivery_state TEXT NOT NULL,attempts INTEGER NOT NULL,ordinal INTEGER NOT NULL UNIQUE,FOREIGN KEY(simulation_run_id) REFERENCES scenario_runs);
"""
_DEFAULT_API_VERSION = object()

TABLES = {"simulator_instances","scenario_runs","provider_objects","idempotency","webhook_outbox"}
MAX_BALANCE = 9007199254740991  # Largest integer exactly representable by common JSON clients.
MAX_ACCOUNTING_OBJECTS = 16384
ACCOUNTING_KINDS = frozenset({"balance","balance_settings","balance_transaction"})


def new_id(prefix):
    return prefix + "_LocalSim" + uuid.uuid4().hex


def return_url(value):
    try:
        parsed = urlsplit(value)
        return bool(parsed.scheme in {"http", "https"} and parsed.hostname and
                    not parsed.username and not parsed.password and parsed.port != 0)
    except ValueError:
        return False


def period_end(timestamp, interval, anchor_day=None):
    start = datetime.fromtimestamp(timestamp,timezone.utc)
    year = start.year + (1 if interval == "year" or start.month == 12 else 0)
    month = start.month if interval == "year" else start.month % 12 + 1
    day = min(anchor_day or start.day,calendar.monthrange(year,month)[1])
    return int(start.replace(year=year,month=month,day=day).timestamp())


def v2_identity_state(verified):
    capability = {"status":"active" if verified else "restricted","status_details":[] if verified else
                  [{"code":"requirements_past_due","resolution":"provide_info"}]}
    entries = [] if verified else [{"awaiting_action_from":"user","description":"synthetic_identity_verification",
        "errors":[],"impact":{"restricts_capabilities":[{"capability":"card_payments","configuration":"merchant",
            "deadline":{"status":"past_due"}}]},"minimum_deadline":{"status":"past_due"},
        "requested_reasons":[{"code":"routine_onboarding"}]}]
    return {"configuration":{"merchant":{"applied":True,"capabilities":{"card_payments":capability}}},
            "requirements":{"entries":entries,"summary":{} if verified else {"minimum_deadline":{"status":"past_due"}}}}


def v2_projection(doc, include):
    result = {key:value for key,value in doc.items() if key not in {"version","identity","configuration","requirements"}}
    result["identity"] = doc["identity"] if "identity" in include else None
    result["configuration"] = doc["configuration"] if "configuration.merchant" in include else None
    result["requirements"] = doc["requirements"] if "requirements" in include else None
    return result


class StateStore:
    def __init__(self, path, create=False, clock=None, api_version=None):
        self.path = Path(path)
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self._fd = None
        self.connection = None
        need(self.path.is_absolute() and self.path.parent == self.path.parent.resolve()
             and not self.path.is_symlink(), 503, "simulator_state")
        need(api_version is None or (isinstance(api_version, str) and api_version in wire.API_PROFILES),
             400, "simulator_api_version")
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            # macOS flock on the database conflicts with SQLite's own lock.
            # Keep the process ownership lock on a permanent sibling inode.
            self.lock_path = self.path.with_name(self.path.name + ".lock")
            self._fd = os.open(self.lock_path, flags, 0o600)
            lock_facts = os.fstat(self._fd)
            need(stat.S_ISREG(lock_facts.st_mode) and lock_facts.st_uid == os.getuid()
                 and stat.S_IMODE(lock_facts.st_mode) == 0o600, 503, "simulator_state")
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_identity = (lock_facts.st_dev, lock_facts.st_ino)
            state_fd = os.open(self.path, flags, 0o600)
            try:
                facts = os.fstat(state_fd)
            finally:
                os.close(state_fd)
            need(stat.S_ISREG(facts.st_mode) and facts.st_uid == os.getuid()
                 and stat.S_IMODE(facts.st_mode) == 0o600, 503, "simulator_state")
            self._identity = (facts.st_dev, facts.st_ino)
            self.connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                              check_same_thread=False, isolation_level=None, timeout=1)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA synchronous=FULL")
            if create:
                self.connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
                instance = str(uuid.uuid4())
                self.connection.execute("INSERT INTO simulator_instances VALUES(?,?,2,?)",
                                        (instance, "acct_LocalSim" + uuid.UUID(instance).hex,
                                         api_version or wire.API_VERSION))
                self.connection.commit()
            need(self.connection.execute("PRAGMA quick_check").fetchone()[0] == "ok", 503, "simulator_state")
            tables = {x[0] for x in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            need(tables == TABLES, 503, "simulator_state")
            rows = self.connection.execute("SELECT * FROM simulator_instances").fetchall()
            need(len(rows) == 1, 503, "simulator_state")
            row = dict(rows[0])
            if row["schema_version"] == 1:
                # Original databases remain Acacia without an implicit schema
                # migration or relabelling existing objects and webhook bytes.
                need(set(row) == {"instance_id", "platform_account", "schema_version"},
                     503, "simulator_state")
                self.api_version = wire.LEGACY_API_VERSION
            else:
                need(row["schema_version"] == 2 and
                     set(row) == {"instance_id", "platform_account", "schema_version", "api_version"},
                     503, "simulator_state")
                self.api_version = row["api_version"]
            need(isinstance(self.api_version, str) and self.api_version in wire.API_PROFILES,
                 503, "simulator_state")
            need(api_version is None or api_version == self.api_version, 409, "simulator_api_version")
            self.protocol_profile = wire.API_PROFILES[self.api_version]
            self.instance_id = rows[0]["instance_id"]
            self.platform_account = rows[0]["platform_account"]
            need(str(uuid.UUID(self.instance_id)) == self.instance_id
                 and self.platform_account == "acct_LocalSim" + uuid.UUID(self.instance_id).hex,
                 503, "simulator_state")
        except (OSError, sqlite3.Error) as error:
            self.close()
            raise SimulatorError(503, "simulator_state") from error
        except BaseException:
            self.close()
            raise

    def __repr__(self):
        return "StateStore(provider resources)"

    def _live(self):
        need(self.connection is not None and self.path.is_file() and not self.path.is_symlink(),
             503, "simulator_state")
        lock_facts = self.lock_path.lstat()
        need(stat.S_ISREG(lock_facts.st_mode)
             and (lock_facts.st_dev, lock_facts.st_ino) == self._lock_identity,
             503, "simulator_state")
        facts = self.path.stat()
        need((facts.st_dev, facts.st_ino) == self._identity and facts.st_size > 0,
             503, "simulator_state")

    @contextmanager
    def transaction(self):
        with self._lock:
            self._live()
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def close(self):
        with self._lock:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def _run(self, run, active=True, status=409):
        need(isinstance(run, str) and len(run) == 36, status, "simulator_run")
        row = self.connection.execute("SELECT * FROM scenario_runs WHERE simulation_run_id=?", (run,)).fetchone()
        need(row is not None and (not active or not row["retired"]), status, "simulator_run")
        return dict(row)

    def run(self, run, active=True, status=409):
        with self.transaction():
            return self._run(run, active, status)

    def _new_run(self, scenario):
        need(isinstance(scenario,str) and scenario in wire.SCENARIOS)
        need(self.connection.execute("SELECT count(*) FROM scenario_runs").fetchone()[0] < 64,
             429, "simulator_limit")
        run = str(uuid.uuid4())
        self.connection.execute("INSERT INTO scenario_runs VALUES(?,?,1,0,?)",
                                (run, scenario, wire.dumps(scenarios.initial(scenario)).decode()))
        return self._run(run)

    def create_run(self, scenario):
        with self.transaction():
            return self._new_run(scenario)

    def reset_run(self, run, version):
        with self.transaction():
            row = self._run(run)
            need(type(version) is int and version == row["state_version"], 409, "simulator_version")
            fresh = self._new_run(row["scenario_id"])
            self.connection.execute("UPDATE scenario_runs SET retired=1,state_version=state_version+1 WHERE simulation_run_id=?", (run,))
            return fresh

    def _object(self, run, kind, object_id, account=None):
        row = self.connection.execute("SELECT * FROM provider_objects WHERE simulation_run_id=? AND kind=? AND object_id=?",
                                      (run,kind,object_id)).fetchone()
        need(row is not None, 403, "simulator_resource")
        need(account is None or row["account_id"] == account, 403, "simulator_resource")
        doc = json.loads(row["document"])
        doc["version"] = row["version"]
        return doc

    def object(self, run, kind, object_id, account=None):
        with self.transaction():
            self._run(run, active=False)
            return self._object(run,kind,object_id,account)

    def objects(self, run, kind):
        with self.transaction():
            self._run(run, active=False)
            return [json.loads(row[0]) for row in self.connection.execute(
                "SELECT document FROM provider_objects WHERE simulation_run_id=? AND kind=? ORDER BY object_id", (run,kind))]

    def _reservations(self):
        rows = self.connection.execute(
            "SELECT p.kind,p.document FROM provider_objects p JOIN scenario_runs r ON p.simulation_run_id=r.simulation_run_id "
            "WHERE p.kind IN ('payout','refund','dispute','subscription','payment_intent') AND r.retired=0").fetchall()
        events = accounting = resources = 0
        for row in rows:
            doc = json.loads(row["document"])
            if row["kind"] == "payout" and doc.get("status") == "pending":
                events += 1
            elif row["kind"] == "refund" and doc.get("status") == "pending":
                events += 1
                accounting += 2
            elif row["kind"] == "dispute" and doc.get("status") == "needs_response":
                events += 1
                accounting += 1
            elif row["kind"] == "subscription" and doc.get("local_open_invoice"):
                events += 4
                accounting += 4 if doc.get("application_fee_percent") else 2
                resources += 4
            elif row["kind"] == "payment_intent" and doc.get("payment_method_types") == ["us_bank_account"]:
                if doc.get("status") == "requires_action":
                    events += 4
                    accounting += 4 if doc.get("application_fee_amount") else 2
                    resources += 2
                elif doc.get("status") == "processing":
                    events += 2
                    accounting += 4 if doc.get("application_fee_amount") else 2
                    resources += 2
        return events,accounting,resources

    def _put(self, run, account, kind, document):
        doc = dict(document)
        old = self.connection.execute("SELECT version FROM provider_objects WHERE simulation_run_id=? AND kind=? AND object_id=?",
                                      (run,kind,doc["id"])).fetchone()
        if not old:
            if kind in ACCOUNTING_KINDS:
                count = self.connection.execute("SELECT count(*) FROM provider_objects WHERE kind IN ('balance','balance_settings','balance_transaction')").fetchone()[0]
                _, reserved, _ = self._reservations()
                need(count + 1 + reserved <= MAX_ACCOUNTING_OBJECTS,429,"simulator_limit")
            else:
                count = self.connection.execute("SELECT count(*) FROM provider_objects WHERE kind NOT IN ('balance','balance_settings','balance_transaction')").fetchone()[0]
                _, _, reserved = self._reservations()
                need(count + 1 + reserved <= wire.MAX_OBJECTS,429,"simulator_limit")
        doc["version"] = old[0] + 1 if old else 1
        encoded = wire.dumps(doc).decode()
        self.connection.execute("INSERT INTO provider_objects VALUES(?,?,?,?,?,?) ON CONFLICT(simulation_run_id,kind,object_id) DO UPDATE SET version=excluded.version,document=excluded.document",
                                (run,account,kind,doc["id"],doc["version"],encoded))
        return doc

    def _public(self, run, doc):
        out = dict(doc)
        out.pop("version", None)
        if out.get("object") == "account" and self.api_version != wire.LEGACY_API_VERSION:
            out.setdefault("type", "standard")
        for key in list(out):
            if key.startswith("local_"):
                out.pop(key)
        if out["object"] == "payment_intent" and out.get("latest_charge"):
            charge = self._object(run,"charge",out["latest_charge"])
            out["latest_charge"] = self._public(run,charge)
        return out

    def _event(self, run, account, kind, document, key=None):
        event_id = "evt_LocalSim" + uuid.uuid4().hex
        payload = wire.dumps({"id":event_id,"object":"event","livemode":False,"api_version":self.api_version,
            "type":kind,"account":account,"created":int(self.clock()),
            "data":{"object":self._public(run,document)},"request":{"idempotency_key":key},
            "local_simulator":{"instance_id":self.instance_id,"simulation_run_id":run}})
        self._enqueue(run,event_id,payload)

    def _v2_event(self,run,account_id,kind):
        event_id = "evt_LocalSim" + uuid.uuid4().hex
        created = datetime.fromtimestamp(self.clock(),timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
        payload = wire.dumps({"id":event_id,"object":"v2.core.event","type":kind,"created":created,"livemode":False,
            "related_object":{"id":account_id,"type":"v2.core.account","url":"/v2/core/accounts/"+account_id}})
        self._enqueue(run,event_id,payload)

    def _enqueue(self,run,event_id,payload):
        count = self.connection.execute("SELECT count(*) FROM webhook_outbox").fetchone()[0]
        reserved, accounting_reserved, resources_reserved = self._reservations()
        accounting_count = self.connection.execute("SELECT count(*) FROM provider_objects WHERE kind IN ('balance','balance_settings','balance_transaction')").fetchone()[0]
        resource_count = self.connection.execute("SELECT count(*) FROM provider_objects WHERE kind NOT IN ('balance','balance_settings','balance_transaction')").fetchone()[0]
        need(count + 1 + reserved <= wire.MAX_EVENTS and accounting_count + accounting_reserved <= MAX_ACCOUNTING_OBJECTS
             and resource_count + resources_reserved <= wire.MAX_OBJECTS,429,"simulator_limit")
        ordinal = self.connection.execute("SELECT COALESCE(max(ordinal),0)+1 FROM webhook_outbox").fetchone()[0]
        self.connection.execute("INSERT INTO webhook_outbox VALUES(?,?,?,?,?,'queued',0,?)",
            (str(uuid.uuid4()),event_id,run,payload,hashlib.sha256(payload).hexdigest(),ordinal))

    def events(self, run):
        with self.transaction():
            return [dict(x) for x in self.connection.execute("SELECT * FROM webhook_outbox WHERE simulation_run_id=? ORDER BY ordinal",(run,))]

    def reserve_event(self, run):
        with self.transaction():
            self._run(run)
            row = self.connection.execute("SELECT * FROM webhook_outbox WHERE simulation_run_id=? AND (delivery_state IN('queued','failed','sending') OR delivery_state LIKE 'retry:%') AND attempts<1000 ORDER BY ordinal LIMIT 1",(run,)).fetchone()
            if row is None:
                return None
            self.connection.execute("UPDATE webhook_outbox SET delivery_state='sending',attempts=attempts+1 WHERE delivery_id=?",(row["delivery_id"],))
            result = dict(row)
            result["attempts"] += 1
            return result

    def reserve_automatic_event(self,max_attempts=1):
        """Claim queued or due retry events, never uncertain in-flight attempts."""
        with self.transaction():
            self.connection.execute(
                "UPDATE webhook_outbox SET delivery_state='failed' WHERE delivery_state LIKE 'retry:%' AND attempts>=? "
                "AND simulation_run_id IN (SELECT simulation_run_id FROM scenario_runs WHERE retired=0)",(max_attempts,))
            row = self.connection.execute(
                "SELECT e.* FROM webhook_outbox e JOIN scenario_runs r ON r.simulation_run_id=e.simulation_run_id "
                "WHERE r.retired=0 AND e.attempts<? AND (e.delivery_state='queued' OR "
                "(e.delivery_state LIKE 'retry:%' AND CAST(substr(e.delivery_state,7) AS INTEGER)<=?)) "
                "ORDER BY e.ordinal LIMIT 1",(max_attempts,int(self.clock()))
            ).fetchone()
            if row is None:
                return None
            self.connection.execute("UPDATE webhook_outbox SET delivery_state='sending',attempts=attempts+1 WHERE delivery_id=?",(row["delivery_id"],))
            result = dict(row)
            result["attempts"] += 1
            return result

    def delivery_page(self, run, after_ordinal=0):
        need(type(after_ordinal) is int and after_ordinal >= 0)
        with self.transaction():
            self._run(run)
            rows = self.connection.execute(
                "SELECT delivery_id,event_id,payload,delivery_state,attempts,ordinal FROM webhook_outbox "
                "WHERE simulation_run_id=? AND ordinal>? ORDER BY ordinal LIMIT 51",
                (run,after_ordinal)).fetchall()
            page = []
            for row in rows[:50]:
                payload = json.loads(bytes(row["payload"]))
                resource = payload.get("related_object") if payload.get("object") == "v2.core.event" else payload["data"]["object"]
                state = row["delivery_state"]
                item = {"delivery_id":row["delivery_id"],"event_id":row["event_id"],"type":payload["type"],
                    "resource_id":resource.get("id"),"delivery_state":"retry_scheduled" if state.startswith("retry:") else state,
                    "attempts":row["attempts"],"ordinal":row["ordinal"]}
                if state.startswith("retry:"):
                    item["next_retry_at"] = int(state[6:])
                page.append(item)
            return {"events":page,"next_after":page[-1]["ordinal"] if len(rows)>50 else None}

    def reserve_selected_event(self, run, delivery_id, version):
        need(isinstance(delivery_id, str) and len(delivery_id) == 36)
        with self.transaction():
            current = self._run(run)
            need(type(version) is int and version == current["state_version"], 409, "simulator_version")
            row = self.connection.execute(
                "SELECT * FROM webhook_outbox WHERE simulation_run_id=? AND delivery_id=? "
                "AND (delivery_state IN('queued','failed','sending') OR delivery_state LIKE 'retry:%') AND attempts<1000", (run,delivery_id)).fetchone()
            need(row is not None, 409, "simulator_event")
            self.connection.execute("UPDATE webhook_outbox SET delivery_state='sending',attempts=attempts+1 "
                                    "WHERE delivery_id=?", (delivery_id,))
            self.connection.execute("UPDATE scenario_runs SET state_version=state_version+1 WHERE simulation_run_id=?", (run,))
            reserved = dict(row)
            reserved["attempts"] += 1
            return self._run(run), reserved

    def _refresh_refunded_charge(self, run, account, refund):
        charge = self._object(run,"charge",refund["charge"],account)
        self._assert_refundable(run,account,refund["charge"])
        rows = self.connection.execute(
            "SELECT document FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind='refund'",
            (run,account)).fetchall()
        docs = [json.loads(row[0]) for row in rows]
        total = sum(doc["amount"] for doc in docs if doc["charge"] == charge["id"] and doc["status"] == "succeeded")
        need(total <= charge["amount_captured"],409,"simulator_conservation")
        delta = total - charge.get("amount_refunded",0)
        need(delta >= 0,409,"simulator_conservation")
        if delta:
            transaction = self._balance_debit(run,account,refund["id"],delta,"refund",self._charge_source(charge))
            refund["balance_transaction"] = None if self.api_version == wire.LEGACY_API_VERSION else transaction
            refund = self._put(run,account,"refund",refund)
        charge["amount_refunded"] = total
        charge["refunded"] = total == charge["amount_captured"]
        self._put(run,account,"charge",charge)
        return refund

    def _assert_refundable(self,run,account,charge_id):
        disputes = [json.loads(row[0]) for row in self.connection.execute(
            "SELECT document FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind='dispute'",(run,account))]
        need(not any(d["charge"] == charge_id and d["status"] != "won" for d in disputes),409,"simulator_state")

    def refund_action(self, run, account, refund_id, action, version):
        with self.transaction():
            self._run(run)
            need(action in {"succeed", "fail", "cancel"}, 400, "simulator_unsupported")
            account = self._billing_scope(run,account)
            refund = self._object(run,"refund",refund_id,account)
            need(type(version) is int and version == refund["version"],409,"simulator_version")
            need(refund["status"] == "pending",409,"simulator_state")
            refund["status"] = {"succeed":"succeeded", "fail":"failed", "cancel":"canceled"}[action]
            if action == "fail":
                refund["failure_reason"] = "declined"
            refund = self._put(run,account,"refund",refund)
            if action == "succeed":
                refund = self._refresh_refunded_charge(run,account,refund)
            self._event(run,account,"refund.failed" if action == "fail" else "refund.updated",refund)
            return {"provider_version":refund["version"],"status":refund["status"]}

    def account_readiness(self, run, account, action, version):
        changes = {"enable_charges":("charges_enabled",True), "disable_charges":("charges_enabled",False),
                   "enable_payouts":("payouts_enabled",True), "disable_payouts":("payouts_enabled",False),
                   "complete_details":("details_submitted",True), "require_details":("details_submitted",False)}
        need(action in changes,400,"simulator_unsupported")
        with self.transaction():
            self._run(run)
            doc = self._object(run,"account",account,account)
            need(type(version) is int and version == doc["version"],409,"simulator_version")
            field, value = changes[action]
            doc[field] = value
            if field == "charges_enabled":
                doc["capabilities"]["card_payments"] = "active" if value else "inactive"
            doc["requirements"]["disabled_reason"] = None if all(
                doc[key] for key in ("charges_enabled","payouts_enabled","details_submitted")) else "requirements.past_due"
            doc = self._put(run,account,"account",doc)
            self._event(run,account,"account.updated",doc)
            return {"provider_version":doc["version"],"account":account}

    def _billing_scope(self, run, account):
        scope = self.platform_account if account is None else account
        if scope != self.platform_account:
            need(isinstance(scope,str) and scope.startswith("acct_") and wire.ID.fullmatch(scope),403,"simulator_resource")
            self._object(run,"account",scope,scope)
        return scope

    def _balance(self, run, scope):
        balance_id = "bal_" + scope.removeprefix("acct_")
        row = self.connection.execute(
            "SELECT document FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind='balance' AND object_id=?",
            (run,scope,balance_id)).fetchone()
        doc = json.loads(row[0]) if row else {"id":balance_id,"object":"local_balance","available":0}
        if "local_sources" not in doc:
            doc["local_sources"] = {"card":doc["available"],"bank_account":0}
        need(set(doc["local_sources"]) == {"card","bank_account"} and
             all(type(value) is int for value in doc["local_sources"].values()) and
             sum(doc["local_sources"].values()) == doc["available"],503,"simulator_state")
        return doc

    def _adjust_available(self,balance,source,amount):
        need(source in {"card","bank_account"},503,"simulator_state")
        balance["local_sources"][source] += amount
        balance["available"] = sum(balance["local_sources"].values())
        need(-MAX_BALANCE <= balance["available"] <= MAX_BALANCE and
             -MAX_BALANCE <= balance["local_sources"][source] <= MAX_BALANCE,429,"simulator_limit")

    def _charge_source(self,charge):
        return "bank_account" if charge.get("payment_method_details",{}).get("type") == "us_bank_account" else "card"

    def _pending_balance(self,run,scope,source=None):
        return sum(doc["net"] for row in self.connection.execute(
            "SELECT document FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind='balance_transaction'",(run,scope))
            if (doc := json.loads(row[0]))["status"] == "pending" and (source is None or doc.get("source_type","card") == source))

    def _balance_settings(self,run,scope):
        row = self.connection.execute("SELECT document FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind='balance_settings'",(run,scope)).fetchone()
        return json.loads(row[0]) if row else {"fee_bps":0,"fee_flat":0,"settlement_delay":172800}

    def configure_balance(self,run,account,fee_bps,fee_flat,settlement_delay,version):
        bps = wire.integer(fee_bps,0,10000)
        flat = wire.integer(fee_flat,0,99999999)
        delay = wire.integer(settlement_delay,0,2592000)
        with self.transaction():
            row = self._run(run)
            need(type(version) is int and version == row["state_version"],409,"simulator_version")
            scope = self._billing_scope(run,account)
            self._put(run,scope,"balance_settings",{"id":"balset_"+scope.removeprefix("acct_"),
                "object":"local_balance_settings","fee_bps":bps,"fee_flat":flat,"settlement_delay":delay})
            self.connection.execute("UPDATE scenario_runs SET state_version=state_version+1 WHERE simulation_run_id=?",(run,))
            return {"state_version":version+1}

    def _balance_entry(self,run,scope,source,amount,fee,available_on,status="pending",category="charge",source_type="card"):
        net = amount-fee
        balance = self._balance(run,scope)
        pending = self._pending_balance(run,scope)
        payouts = [doc for row in self.connection.execute(
            "SELECT document FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind='payout'",(run,scope))
            if (doc := json.loads(row[0]))["status"] == "pending"]
        reserved = sum(doc["amount"] for doc in payouts)
        source_reserved = sum(doc["amount"] for doc in payouts if doc.get("source_type","card") == source_type)
        source_total = balance["local_sources"][source_type] + self._pending_balance(run,scope,source_type) + source_reserved + net
        need(-MAX_BALANCE <= balance["available"]+pending+reserved+net <= MAX_BALANCE and
             -MAX_BALANCE <= source_total <= MAX_BALANCE,429,"simulator_limit")
        if status == "available":
            self._adjust_available(balance,source_type,net)
            self._put(run,scope,"balance",balance)
        elif "version" not in balance:
            # Allocate the account bucket before accepting pending funds so
            # settlement never needs another accounting object.
            self._put(run,scope,"balance",balance)
        return self._put(run,scope,"balance_transaction",{"id":new_id("txn"),"object":"balance_transaction",
            "livemode":False,"amount":amount,"fee":fee,"net":net,"currency":"usd","source":source,
            "type":category,"source_type":source_type,"status":status,"created":int(self.clock()),"available_on":available_on})

    def _charge_balance(self,run,scope,charge_id,amount,application_fee,fee_id=None,source_type="card"):
        settings = self._balance_settings(run,scope)
        processing = (amount*settings["fee_bps"]+5000)//10000 + settings["fee_flat"]
        need(processing+application_fee <= amount,409,"simulator_insufficient_funds")
        available_on = int(self.clock()) + settings["settlement_delay"]
        transaction = self._balance_entry(run,scope,charge_id,amount,processing+application_fee,available_on,source_type=source_type)
        if application_fee:
            self._balance_entry(run,self.platform_account,fee_id,application_fee,0,available_on,category="application_fee",source_type=source_type)
        return transaction["id"]

    def _balance_debit(self,run,scope,source,amount,category,source_type="card"):
        return self._balance_entry(run,scope,source,-amount,0,int(self.clock()),status="available",category=category,source_type=source_type)["id"]

    def settle_balances(self,run,account,version):
        with self.transaction():
            row = self._run(run)
            need(type(version) is int and version == row["state_version"],409,"simulator_version")
            scope = self._billing_scope(run,account)
            pending = [json.loads(item[0]) for item in self.connection.execute(
                "SELECT document FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind='balance_transaction'",(run,scope))]
            due = [item for item in pending if item["status"] == "pending" and item["available_on"] <= int(self.clock())]
            balance = self._balance(run,scope)
            for item in due:
                self._adjust_available(balance,item.get("source_type","card"),item["net"])
                item["status"] = "available"
                self._put(run,scope,"balance_transaction",item)
            if due:
                self._put(run,scope,"balance",balance)
                self._event(run,scope,"balance.available",{"id":balance["id"],"object":"balance","livemode":False,
                    "available":[{"amount":balance["available"],"currency":"usd"}]})
                self.connection.execute("UPDATE scenario_runs SET state_version=state_version+1 WHERE simulation_run_id=?",(run,))
            return {"state_version":version+(1 if due else 0),"settled":len(due),"available":balance["available"]}

    def v2_identity_action(self,run,account_id,action,version):
        need(action in {"verify","require"},400,"simulator_unsupported")
        with self.transaction():
            self._run(run)
            doc = self._object(run,"account_v2",account_id,account_id)
            need(type(version) is int and version == doc["version"],409,"simulator_version")
            doc.update(v2_identity_state(action == "verify"))
            doc = self._put(run,account_id,"account_v2",doc)
            self._v2_event(run,account_id,"v2.core.account[requirements].updated")
            return {"provider_version":doc["version"],"status":"verified" if action == "verify" else "required"}

    def fund_balance(self,run,account,amount,version):
        amount = wire.integer(amount,1)
        with self.transaction():
            row = self._run(run)
            need(type(version) is int and version == row["state_version"],409,"simulator_version")
            scope = self._billing_scope(run,account)
            balance = self._balance(run,scope)
            payouts = [doc for item in self.connection.execute(
                "SELECT document FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind='payout'",(run,scope))
                if (doc := json.loads(item[0]))["status"] == "pending"]
            reserved = sum(doc["amount"] for doc in payouts)
            card_reserved = sum(doc["amount"] for doc in payouts if doc.get("source_type","card") == "card")
            need(balance["available"] + reserved + self._pending_balance(run,scope) + amount <= MAX_BALANCE and
                 balance["local_sources"]["card"] + card_reserved + self._pending_balance(run,scope,"card") + amount <= MAX_BALANCE,429,"simulator_limit")
            self._adjust_available(balance,"card",amount)
            self._put(run,scope,"balance",balance)
            self.connection.execute("UPDATE scenario_runs SET state_version=state_version+1 WHERE simulation_run_id=?",(run,))
            return {"state_version":version+1,"available":balance["available"],"currency":"usd"}

    def _tax_settings(self,run,scope):
        row = self.connection.execute("SELECT document FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind='tax_settings'",(run,scope)).fetchone()
        return json.loads(row[0]) if row else None

    def configure_tax(self,run,account,rate_bps,version):
        rate = wire.integer(rate_bps,0,10000)
        with self.transaction():
            row = self._run(run)
            need(type(version) is int and version == row["state_version"],409,"simulator_version")
            scope = self._billing_scope(run,account)
            doc = {"id":"tax_" + scope.removeprefix("acct_"),"object":"local_tax_settings","rate_bps":rate}
            self._put(run,scope,"tax_settings",doc)
            self.connection.execute("UPDATE scenario_runs SET state_version=state_version+1 WHERE simulation_run_id=?",(run,))
            return {"state_version":version+1,"rate_bps":rate}

    def _transition_payout(self,run,scope,payout,action,key=None):
        need(payout["status"] == "pending",409,"simulator_state")
        need(action in {"pay","fail","cancel"},400,"simulator_unsupported")
        if action in {"fail","cancel"}:
            balance = self._balance(run,scope)
            need(balance["available"] + payout["amount"] <= MAX_BALANCE,429,"simulator_limit")
            self._adjust_available(balance,payout.get("source_type","card"),payout["amount"])
            self._put(run,scope,"balance",balance)
        payout["status"] = {"pay":"paid","fail":"failed","cancel":"canceled"}[action]
        if action == "fail":
            payout["failure_code"] = "account_closed"
        payout = self._put(run,scope,"payout",payout)
        self._event(run,scope,"payout." + payout["status"],payout,key)
        return payout

    def payout_action(self,run,account,payout_id,action,version):
        with self.transaction():
            self._run(run)
            scope = self._billing_scope(run,account)
            payout = self._object(run,"payout",payout_id,scope)
            need(type(version) is int and version == payout["version"],409,"simulator_version")
            payout = self._transition_payout(run,scope,payout,action)
            return {"provider_version":payout["version"],"status":payout["status"]}

    def seed_subscription(self, run, account, client_reference_id, metadata, amount, version, interval="year"):
        need(client_reference_id is None or (isinstance(client_reference_id,str) and
             re.fullmatch(r"[A-Za-z0-9_-]{1,64}",client_reference_id) is not None))
        need(isinstance(metadata,dict) and len(metadata) <= 16)
        for key,value in metadata.items():
            need(isinstance(key,str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}",key) is not None)
            need(isinstance(value,str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}",value) is not None)
        amount = wire.integer(amount,50)
        need(interval in {"month","year"})
        with self.transaction():
            row = self._run(run)
            need(type(version) is int and version == row["state_version"],409,"simulator_version")
            scope = self._billing_scope(run,account)
            now = int(self.clock())
            end = period_end(now,interval)
            customer, product, price = (new_id(x) for x in ("cus","prod","price"))
            subscription, invoice, session = (new_id(x) for x in ("sub","in","cs"))
            intent, charge, line, payment = (new_id(x) for x in ("pi","ch","il","inpay"))
            self._put(run,scope,"customer",{"id":customer,"object":"customer","livemode":False,
                "email":"buyer@example.test","created":now,"metadata":{}})
            self._put(run,scope,"product",{"id":product,"object":"product","livemode":False,
                "name":"Synthetic product","active":True,"created":now})
            self._put(run,scope,"price",{"id":price,"object":"price","livemode":False,"product":product,
                "active":True,"type":"recurring","currency":"usd","unit_amount":amount,
                "recurring":{"interval":interval,"interval_count":1},"created":now})
            sub = self._put(run,scope,"subscription",{"id":subscription,"object":"subscription","livemode":False,
                "customer":customer,"status":"active","current_period_end":end,"created":now,
                "metadata":dict(metadata),"items":{"object":"list","data":[
                    {"id":new_id("si"),"object":"subscription_item","price":{"id":price},"current_period_end":end}]}})
            session_doc = self._put(run,scope,"checkout_session",{"id":session,"object":"checkout.session",
                "livemode":False,"mode":"subscription","status":"complete","payment_status":"paid",
                "customer":customer,"subscription":subscription,"client_reference_id":client_reference_id,"created":now})
            invoice_doc = self._put(run,scope,"invoice",{"id":invoice,"object":"invoice","livemode":False,
                "customer":customer,"status":"paid","amount_paid":amount,"currency":"usd","created":now,
                "status_transitions":{"paid_at":now},"parent":{"subscription_details":{"subscription":subscription}}})
            self._put(run,scope,"payment_intent",{"id":intent,"object":"payment_intent","livemode":False,
                "customer":customer,"amount":amount,"amount_received":amount,"currency":"usd","capture_method":"automatic",
                "payment_method_types":["card"],"status":"succeeded","latest_charge":charge,"created":now})
            balance_tx = self._charge_balance(run,scope,charge,amount,0)
            self._put(run,scope,"charge",{"id":charge,"object":"charge","livemode":False,"customer":customer,
                "amount":amount,"amount_captured":amount,"amount_refunded":0,"captured":True,"paid":True,
                "disputed":False,"currency":"usd","payment_intent":intent,
                "balance_transaction":None if self.api_version == wire.LEGACY_API_VERSION else balance_tx,"created":now})
            self._put(run,scope,"invoice_line",{"id":line,"object":"line_item","invoice":invoice,"amount":amount,
                "pricing":{"price_details":{"price":price}},"parent":{"subscription_item_details":{}},
                "period":{"start":now,"end":end}})
            self._put(run,scope,"invoice_payment",{"id":payment,"object":"invoice_payment","livemode":False,
                "invoice":invoice,"status":"paid","payment":{"type":"payment_intent","payment_intent":intent},
                "amount_paid":amount,"currency":"usd","created":now})
            self._event(run,scope,"checkout.session.completed",session_doc)
            self._event(run,scope,"customer.subscription.updated",sub)
            self._event(run,scope,"invoice.paid",invoice_doc)
            self.connection.execute("UPDATE scenario_runs SET state_version=state_version+1 WHERE simulation_run_id=?",(run,))
            return {"state_version":version+1,"customer":customer,"product":product,"price":price,
                    "subscription":subscription,"invoice":invoice,"session":session,"payment_intent":intent,"charge":charge}

    def seed_promotion(self,run,account,code,percent_off,duration,version):
        need(isinstance(code,str) and re.fullmatch(r"LOCAL_[A-Z0-9_]{1,48}",code) is not None)
        percent = wire.integer(percent_off,1,99)
        need(duration in {"once","forever"})
        with self.transaction():
            row = self._run(run)
            need(type(version) is int and version == row["state_version"],409,"simulator_version")
            scope = self._billing_scope(run,account)
            previous = self.connection.execute("SELECT document FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind='promotion_code'",(run,scope)).fetchall()
            need(not any(json.loads(item[0])["code"] == code for item in previous),409,"simulator_conflict")
            promotion = self._put(run,scope,"promotion_code",{"id":new_id("promo"),"object":"promotion_code",
                "livemode":False,"active":True,"code":code,"created":int(self.clock()),
                "promotion":{"type":"coupon","coupon":{"id":new_id("coupon"),"object":"coupon",
                    "percent_off":percent,"duration":duration,"valid":True}}})
            self.connection.execute("UPDATE scenario_runs SET state_version=state_version+1 WHERE simulation_run_id=?",(run,))
            return {"state_version":version+1,"promotion_code":promotion["id"]}

    def portal_view(self, run, account, portal_id):
        with self.transaction():
            self._run(run)
            scope = self._billing_scope(run,account)
            portal = self._object(run,"portal_session",portal_id,scope)
            config = self._object(run,"portal_configuration",portal["configuration"],scope) if portal.get("configuration") else None
            enabled = config is None or (config["active"] and config["features"]["subscription_cancel"]["enabled"])
            show_invoices = config is None or config["features"]["invoice_history"]["enabled"]
            rows = self.connection.execute("SELECT kind,document,version FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind IN ('subscription','invoice')",(run,scope)).fetchall()
            documents = []
            for row in rows:
                doc = json.loads(row["document"])
                if doc.get("customer") == portal["customer"] and (row["kind"] != "invoice" or show_invoices):
                    doc["version"] = row["version"]
                    documents.append(doc)
            return portal, documents, enabled

    def portal_cancel(self, run, account, portal_id, subscription, action, version):
        need(action in {"cancel","resume"})
        with self.transaction():
            self._run(run)
            scope = self._billing_scope(run,account)
            portal = self._object(run,"portal_session",portal_id,scope)
            if portal.get("configuration"):
                config = self._object(run,"portal_configuration",portal["configuration"],scope)
                need(config["active"] and config["features"]["subscription_cancel"]["enabled"],403,"simulator_resource")
            doc = self._object(run,"subscription",subscription,scope)
            need(doc["customer"] == portal["customer"],403,"simulator_resource")
            need(type(version) is int and version == doc["version"],409,"simulator_version")
            need(doc["status"] != "canceled" and not doc.get("local_open_invoice") and int(self.clock()) < doc["current_period_end"],409,"simulator_state")
            need(bool(doc.get("cancel_at_period_end")) != (action == "cancel"),409,"simulator_state")
            doc["cancel_at_period_end"] = action == "cancel"
            doc["cancel_at"] = doc["current_period_end"] if action == "cancel" else None
            doc = self._put(run,scope,"subscription",doc)
            self._event(run,scope,"customer.subscription.updated",doc)
            return doc["version"]

    def subscription_action(self, run, account, subscription, action, version):
        statuses = {"payment_failed":"past_due", "recover":"active", "cancel":"canceled"}
        need(action in set(statuses) | {"renew","renew_fail","retry_renewal"},400,"simulator_unsupported")
        with self.transaction():
            self._run(run)
            scope = self._billing_scope(run,account)
            doc = self._object(run,"subscription",subscription,scope)
            need(type(version) is int and version == doc["version"],409,"simulator_version")
            need(doc["status"] != "canceled",409,"simulator_state")
            if action in {"renew","renew_fail","retry_renewal"}:
                if action != "retry_renewal" and doc.get("cancel_at_period_end"):
                    need(not doc.get("local_open_invoice") and int(self.clock()) >= doc["current_period_end"],409,"simulator_state")
                    doc["status"] = "canceled"
                    doc = self._put(run,scope,"subscription",doc)
                    self._event(run,scope,"customer.subscription.deleted",doc)
                    return {"provider_version":doc["version"],"status":doc["status"]}
                return self._renew_subscription(run,scope,doc,action)
            if action in {"recover","cancel"}:
                need(not doc.get("local_open_invoice"),409,"simulator_state")
            doc["status"] = statuses[action]
            doc = self._put(run,scope,"subscription",doc)
            self._event(run,scope,"customer.subscription.deleted" if action == "cancel" else "customer.subscription.updated",doc)
            return {"provider_version":doc["version"],"status":doc["status"]}

    def _renew_subscription(self,run,scope,sub,action):
        self._account_ready(run,scope)
        if action == "retry_renewal":
            need(sub.get("local_open_invoice") is not None,409,"simulator_state")
            invoice = self._object(run,"invoice",sub["local_open_invoice"],scope)
            need(invoice["status"] == "open",409,"simulator_state")
            # Free the reserved event capacity inside this transaction; any
            # subsequent failure rolls this change back with the invoice.
            sub.pop("local_open_invoice")
            sub = self._put(run,scope,"subscription",sub)
        else:
            need(not sub.get("local_open_invoice") and int(self.clock()) >= sub["current_period_end"],409,"simulator_state")
            if sub.get("local_promotion_duration") == "once":
                for discount_id in sub.get("discounts",[]):
                    discount_doc = self._object(run,"discount",discount_id,scope)
                    discount_doc["end"] = sub["current_period_end"]
                    self._put(run,scope,"discount",discount_doc)
                sub["discounts"] = []
                sub["local_promotion_duration"] = None
                sub["local_promotion_percent"] = 0
            price_id = sub["items"]["data"][0]["price"]["id"]
            price = self._object(run,"price",price_id,scope)
            need(price.get("type") == "recurring" and price.get("active"),409,"simulator_state")
            start = sub["current_period_end"]
            anchor_day = datetime.fromtimestamp(sub["created"],timezone.utc).day
            end = period_end(start,price["recurring"]["interval"],anchor_day)
            subtotal = price["unit_amount"]
            percent = sub.get("local_promotion_percent",0) if sub.get("local_promotion_duration") == "forever" else 0
            discount = (subtotal * percent + 50) // 100
            rate = sub.get("local_tax_rate_bps")
            tax = ((subtotal-discount)*rate+5000)//10000 if rate is not None else 0
            total = subtotal-discount+tax
            need(0 < total <= 99999999,400,"simulator_unsupported")
            now = int(self.clock())
            invoice = self._put(run,scope,"invoice",{"id":new_id("in"),"object":"invoice","livemode":False,
                "customer":sub["customer"],"status":"open","amount_paid":0,"amount_due":total,"amount_remaining":total,
                "subtotal":subtotal,"total":total,"currency":"usd","created":now,"billing_reason":"subscription_cycle",
                "discounts":list(sub.get("discounts",[])) if percent else [],
                "total_discount_amounts":[{"amount":discount,"discount":sub["discounts"][0]}] if discount else [],
                "automatic_tax":{"enabled":rate is not None,"status":"complete" if rate is not None else None},
                "status_transitions":{"paid_at":None},"parent":{"subscription_details":{"subscription":sub["id"]}},
                "local_period_start":start,"local_period_end":end,"local_price_id":price_id})
            self._put(run,scope,"invoice_line",{"id":new_id("il"),"object":"line_item","invoice":invoice["id"],
                "amount":subtotal,"pricing":{"price_details":{"price":price_id}},
                "parent":{"subscription_item_details":{}},"period":{"start":start,"end":end}})
            self._event(run,scope,"invoice.created",invoice)
            if action == "renew_fail":
                sub["status"] = "past_due"
                sub["local_open_invoice"] = invoice["id"]
                sub = self._put(run,scope,"subscription",sub)
                self._event(run,scope,"invoice.payment_failed",invoice)
                self._event(run,scope,"customer.subscription.updated",sub)
                return {"provider_version":sub["version"],"status":sub["status"],"invoice":invoice["id"]}
        amount = invoice["total"]
        now = int(self.clock())
        intent, charge = new_id("pi"), new_id("ch")
        self._put(run,scope,"payment_intent",{"id":intent,"object":"payment_intent","livemode":False,
            "customer":sub["customer"],"amount":amount,"amount_received":amount,"currency":"usd",
            "capture_method":"automatic","payment_method_types":["card"],"status":"succeeded","latest_charge":charge,"created":now})
        fee_percent = sub.get("application_fee_percent") or 0
        fee_amount = int((Decimal(amount)*Decimal(str(fee_percent))/100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
        fee_id = new_id("fee") if fee_amount else None
        balance_tx = self._charge_balance(run,scope,charge,amount,fee_amount,fee_id)
        self._put(run,scope,"charge",{"id":charge,"object":"charge","livemode":False,"customer":sub["customer"],
            "amount":amount,"amount_captured":amount,"amount_refunded":0,"captured":True,"paid":True,
            "disputed":False,"currency":"usd","payment_intent":intent,
            "balance_transaction":None if self.api_version == wire.LEGACY_API_VERSION else balance_tx,
            "application_fee":fee_id,"application_fee_amount":fee_amount,"created":now})
        if fee_id:
            fee = self._put(run,scope,"application_fee",{"id":fee_id,"object":"application_fee","livemode":False,
                "account":scope,"charge":charge,"amount":fee_amount,"currency":"usd","balance_transaction":None,"created":now})
            self._event(run,scope,"application_fee.created",fee)
        self._put(run,scope,"invoice_payment",{"id":new_id("inpay"),"object":"invoice_payment","livemode":False,
            "invoice":invoice["id"],"status":"paid","payment":{"type":"payment_intent","payment_intent":intent},
            "amount_paid":amount,"currency":"usd","created":now})
        invoice.update(status="paid",amount_paid=amount,amount_remaining=0,status_transitions={"paid_at":now})
        invoice = self._put(run,scope,"invoice",invoice)
        sub["status"] = "active"
        sub["current_period_end"] = invoice["local_period_end"]
        sub["items"]["data"][0]["current_period_end"] = invoice["local_period_end"]
        sub.pop("local_open_invoice",None)
        sub = self._put(run,scope,"subscription",sub)
        self._event(run,scope,"invoice.payment_succeeded",invoice)
        self._event(run,scope,"invoice.paid",invoice)
        self._event(run,scope,"customer.subscription.updated",sub)
        return {"provider_version":sub["version"],"status":sub["status"],"invoice":invoice["id"]}

    def checkout_action(self, run, account, session_id, action, version):
        need(action in {"complete", "expire"}, 400, "simulator_unsupported")
        with self.transaction():
            self._run(run)
            scope = self._billing_scope(run, account)
            session = self._object(run, "checkout_session", session_id, scope)
            need(type(version) is int and version == session["version"], 409, "simulator_version")
            need(session["status"] == "open", 409, "simulator_state")
            if action == "expire":
                session["status"] = "expired"
                session["url"] = None
                session = self._put(run, scope, "checkout_session", session)
                self._event(run, scope, "checkout.session.expired", session)
                return {"provider_version":session["version"], "status":session["status"]}
            need(self.clock() < session["expires_at"], 409, "simulator_state")
            self._account_ready(run,scope)
            customer = session["customer"]
            if customer is None:
                customer = new_id("cus")
                self._put(run, scope, "customer", {"id":customer, "object":"customer", "livemode":False,
                    "email":session.get("customer_email") or "buyer@example.test", "created":int(self.clock()), "metadata":{}})
            else:
                self._object(run, "customer", customer, scope)
            price = self._object(run, "price", session["local_price"], scope)
            need(price.get("active") and price.get("type") == "recurring", 409, "simulator_state")
            subtotal = price["unit_amount"]
            discount = (subtotal * session.get("local_promotion_percent",0) + 50) // 100
            rate_bps = session.get("local_tax_rate_bps")
            tax_amount = ((subtotal-discount) * rate_bps + 5000) // 10000 if rate_bps is not None else 0
            amount = subtotal-discount + tax_amount
            need(0 < amount <= 99999999,400,"simulator_unsupported")
            now = int(self.clock())
            end = period_end(now,price["recurring"]["interval"])
            subscription, invoice, intent, charge, line, payment = (new_id(x) for x in ("sub","in","pi","ch","il","inpay"))
            for discount_id in session.get("discounts",[]):
                discount_doc = self._object(run,"discount",discount_id,scope)
                discount_doc.update(customer=customer,subscription=subscription)
                self._put(run,scope,"discount",discount_doc)
            sub = self._put(run,scope,"subscription",{"id":subscription,"object":"subscription","livemode":False,
                "customer":customer,"status":"active","current_period_end":end,"created":now,
                "application_fee_percent":session.get("local_fee_percent"),
                "local_promotion_percent":session.get("local_promotion_percent",0),
                "local_promotion_duration":session.get("local_promotion_duration"),
                "local_tax_rate_bps":rate_bps,
                "discounts":list(session.get("discounts",[])),
                "metadata":session["local_subscription_metadata"],"items":{"object":"list","data":[
                    {"id":new_id("si"),"object":"subscription_item","price":{"id":price["id"]},"current_period_end":end}]}})
            invoice_doc = self._put(run,scope,"invoice",{"id":invoice,"object":"invoice","livemode":False,
                "customer":customer,"status":"paid","amount_paid":amount,"subtotal":subtotal,"total":amount,
                "discounts":list(session.get("discounts",[])),
                "total_discount_amounts":[{"amount":discount,"discount":session["discounts"][0]}] if discount else [],
                "automatic_tax":{"enabled":rate_bps is not None,"status":"complete" if rate_bps is not None else None},
                "currency":"usd","created":now,
                "status_transitions":{"paid_at":now},"parent":{"subscription_details":{"subscription":subscription}}})
            self._put(run,scope,"payment_intent",{"id":intent,"object":"payment_intent","livemode":False,
                "customer":customer,"amount":amount,"amount_received":amount,"currency":"usd","capture_method":"automatic",
                "payment_method_types":["card"],"status":"succeeded","latest_charge":charge,"created":now})
            percent = session.get("local_fee_percent")
            fee_amount = int((Decimal(amount) * Decimal(str(percent or 0)) / 100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
            fee_id = new_id("fee") if fee_amount else None
            balance_tx = self._charge_balance(run,scope,charge,amount,fee_amount,fee_id)
            self._put(run,scope,"charge",{"id":charge,"object":"charge","livemode":False,"customer":customer,
                "amount":amount,"amount_captured":amount,"amount_refunded":0,"captured":True,"paid":True,
                "disputed":False,"currency":"usd","payment_intent":intent,
                "balance_transaction":None if self.api_version == wire.LEGACY_API_VERSION else balance_tx,
                "application_fee":fee_id,"application_fee_amount":fee_amount,"created":now})
            if fee_id:
                fee_doc = self._put(run,scope,"application_fee",{"id":fee_id,"object":"application_fee","livemode":False,
                    "account":scope,"charge":charge,"amount":fee_amount,"currency":"usd","balance_transaction":None,"created":now})
                self._event(run,scope,"application_fee.created",fee_doc)
            self._put(run,scope,"invoice_line",{"id":line,"object":"line_item","invoice":invoice,"amount":subtotal,
                "pricing":{"price_details":{"price":price["id"]}},"parent":{"subscription_item_details":{}},
                "period":{"start":now,"end":end}})
            self._put(run,scope,"invoice_payment",{"id":payment,"object":"invoice_payment","livemode":False,
                "invoice":invoice,"status":"paid","payment":{"type":"payment_intent","payment_intent":intent},
                "amount_paid":amount,"currency":"usd","created":now})
            session.update({"customer":customer,"subscription":subscription,"status":"complete","payment_status":"paid","url":None,
                "amount_subtotal":subtotal,"amount_total":amount,"total_details":{"amount_tax":tax_amount,"amount_discount":discount},
                "automatic_tax":{"enabled":rate_bps is not None,"status":"complete" if rate_bps is not None else None}})
            session = self._put(run,scope,"checkout_session",session)
            self._event(run,scope,"checkout.session.completed",session)
            self._event(run,scope,"customer.subscription.updated",sub)
            self._event(run,scope,"invoice.paid",invoice_doc)
            return {"provider_version":session["version"],"status":session["status"],"subscription":subscription}

    def _billing_list(self, run, scope, kind, query, path, invoice=None):
        objects = [json.loads(row[0]) for row in self.connection.execute(
            "SELECT document FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind=? ORDER BY object_id",
            (run,scope,kind))]
        if invoice is not None:
            self._object(run,"invoice",invoice,scope)
            objects = [obj for obj in objects if obj.get("invoice") == invoice]
        for key in ("customer","status","invoice","payment_intent","charge","subscription","code"):
            if key in query and not (key == "status" and query[key] == "all"):
                if key == "subscription" and kind == "invoice":
                    objects = [obj for obj in objects if obj.get("parent",{}).get("subscription_details",{}).get("subscription") == query[key]]
                else:
                    objects = [obj for obj in objects if obj.get(key) == query[key]]
        if "active" in query:
            need(query["active"] in {"true","false"})
            objects = [obj for obj in objects if obj.get("active") is (query["active"] == "true")]
        if "payment[type]" in query:
            objects = [obj for obj in objects if obj.get("payment",{}).get("type") == query["payment[type]"]]
        if "payment[payment_intent]" in query:
            objects = [obj for obj in objects if obj.get("payment",{}).get("payment_intent") == query["payment[payment_intent]"]]
        if "starting_after" in query:
            positions = [i for i,obj in enumerate(objects) if obj["id"] == query["starting_after"]]
            need(len(positions) == 1)
            objects = objects[positions[0]+1:]
        limit = wire.integer(query.get("limit","10"),1,100)
        return {"object":"list","data":[self._public(run,obj) for obj in objects[:limit]],
                "has_more":len(objects)>limit,"url":path}

    def settle_event(self, delivery_id, delivered, retry_at=None):
        with self.transaction():
            self.connection.execute("UPDATE webhook_outbox SET delivery_state=? WHERE delivery_id=?",
                                    ("delivered" if delivered else ("retry:"+str(retry_at) if retry_at is not None else "failed"),delivery_id))

    def scenario_action(self, run, action, version):
        with self.transaction():
            row = self._run(run)
            need(type(version) is int and version == row["state_version"], 409, "simulator_version")
            state = scenarios.transition(json.loads(row["state"]),action)
            if action in {"requirements_required","requirements_complete"}:
                for obj in self.connection.execute("SELECT document FROM provider_objects WHERE simulation_run_id=? AND kind='account'",(run,)).fetchall():
                    old = json.loads(obj[0])
                    ready = state["ready"]
                    updated = wire.account_projection(old["id"],old["metadata"],ready,old["created"])
                    updated = self._put(run,old["id"],"account",updated)
                    self._event(run,old["id"],"account.updated",updated)
            elif action == "refund_complete":
                for obj in self.connection.execute("SELECT document,account_id FROM provider_objects WHERE simulation_run_id=? AND kind='refund'",(run,)).fetchall():
                    refund = json.loads(obj["document"])
                    if refund["status"] == "pending":
                        refund["status"] = "succeeded"
                        refund = self._put(run,obj["account_id"],"refund",refund)
                        refund = self._refresh_refunded_charge(run,obj["account_id"],refund)
                        self._event(run,obj["account_id"],"refund.updated",refund)
            elif action == "duplicate_last_event":
                last = self.connection.execute("SELECT * FROM webhook_outbox WHERE simulation_run_id=? ORDER BY ordinal DESC LIMIT 1",(run,)).fetchone()
                need(last is not None, 409, "simulator_event")
                self._enqueue(run,last["event_id"],bytes(last["payload"]))
            self.connection.execute("UPDATE scenario_runs SET state=?,state_version=state_version+1 WHERE simulation_run_id=?",
                                    (wire.dumps(state).decode(),run))
            return self._run(run)

    def _account_ready(self, run, account):
        if account == self.platform_account:
            return None
        need(isinstance(account,str) and wire.ID.fullmatch(account) and account.startswith("acct_"),403,"simulator_resource")
        doc = self._object(run,"account",account,account)
        need(doc["charges_enabled"],409,"simulator_not_ready")
        return doc

    def _capture_payment(self, run, account, doc, key=None):
        need(doc["status"] == "requires_capture",409,"simulator_state")
        charge_id = new_id("ch")
        fee_id = new_id("fee") if doc["application_fee_amount"] else None
        source_type = "bank_account" if doc["payment_method_types"] == ["us_bank_account"] else "card"
        balance_tx = self._charge_balance(run,account,charge_id,doc["amount"],doc["application_fee_amount"],fee_id,source_type)
        charge = self._put(run,account,"charge",{"id":charge_id,"object":"charge","livemode":False,
            "amount":doc["amount"],"amount_captured":doc["amount"],"captured":True,"paid":True,
            "currency":"usd","payment_intent":doc["id"],"payment_method_details":{"type":doc["payment_method_types"][0]},
            "application_fee":fee_id,"application_fee_amount":doc["application_fee_amount"],
            "balance_transaction":None if self.api_version == wire.LEGACY_API_VERSION else balance_tx,
            "metadata":doc["metadata"],"created":int(self.clock())})
        if fee_id:
            fee = self._put(run,account,"application_fee",{"id":fee_id,"object":"application_fee","livemode":False,
                "account":account,"charge":charge_id,"amount":doc["application_fee_amount"],"currency":"usd",
                "balance_transaction":None,"created":int(self.clock())})
            self._event(run,account,"application_fee.created",fee,key)
        doc.update(status="succeeded",amount_received=doc["amount"],latest_charge=charge_id)
        doc = self._put(run,account,"payment_intent",doc)
        self._event(run,account,"payment_intent.succeeded",doc,key)
        return doc

    def bank_payment_action(self,run,account,intent_id,action,version):
        need(action in {"succeed","fail"},400,"simulator_unsupported")
        with self.transaction():
            self._run(run)
            scope = self._billing_scope(run,account)
            intent = self._object(run,"payment_intent",intent_id,scope)
            need(type(version) is int and version == intent["version"],409,"simulator_version")
            need(intent["payment_method_types"] == ["us_bank_account"] and intent["status"] == "processing",409,"simulator_state")
            if action == "succeed":
                intent["status"] = "requires_capture"
                intent = self._put(run,scope,"payment_intent",intent)
                intent = self._capture_payment(run,scope,intent)
            else:
                intent["status"] = "requires_payment_method"
                intent["last_payment_error"] = {"type":"invalid_request_error","code":"debit_not_authorized"}
                intent = self._put(run,scope,"payment_intent",intent)
                if intent.get("mandate"):
                    mandate = self._object(run,"mandate",intent["mandate"],scope)
                    if mandate["status"] != "inactive":
                        mandate["status"] = "inactive"
                        mandate = self._put(run,scope,"mandate",mandate)
                        self._event(run,scope,"mandate.updated",mandate)
                self._event(run,scope,"payment_intent.payment_failed",intent)
            return {"provider_version":intent["version"],"status":intent["status"]}

    def create_dispute(self,run,account,charge_id,version):
        with self.transaction():
            self._run(run)
            scope = self._billing_scope(run,account)
            charge = self._object(run,"charge",charge_id,scope)
            need(type(version) is int and version == charge["version"],409,"simulator_version")
            need(charge.get("paid") and not charge.get("disputed",False),409,"simulator_state")
            amount = charge["amount"] - charge.get("amount_refunded",0)
            need(amount > 0,409,"simulator_state")
            refunds = [json.loads(row[0]) for row in self.connection.execute(
                "SELECT document FROM provider_objects WHERE simulation_run_id=? AND account_id=? AND kind='refund'",(run,scope))]
            need(not any(r["charge"] == charge_id and r["status"] == "pending" for r in refunds),409,"simulator_state")
            charge["disputed"] = True
            charge = self._put(run,scope,"charge",charge)
            dispute_id = new_id("dp")
            transaction = self._balance_debit(run,scope,dispute_id,amount,"dispute",self._charge_source(charge))
            intent = self._object(run,"payment_intent",charge["payment_intent"],scope)
            if intent.get("payment_method_types") == ["us_bank_account"] and intent.get("mandate"):
                mandate = self._object(run,"mandate",intent["mandate"],scope)
                if mandate["status"] != "inactive":
                    mandate["status"] = "inactive"
                    mandate = self._put(run,scope,"mandate",mandate)
                    self._event(run,scope,"mandate.updated",mandate)
            dispute = self._put(run,scope,"dispute",{"id":dispute_id,"object":"dispute","livemode":False,
                "charge":charge_id,"payment_intent":charge["payment_intent"],"amount":amount,"local_balance_debit":transaction,
                "local_source_type":self._charge_source(charge),
                "currency":"usd","status":"needs_response","reason":"general","created":int(self.clock())})
            self._event(run,scope,"charge.dispute.created",dispute)
            return {"dispute":dispute["id"],"provider_version":dispute["version"],"charge_version":charge["version"]}

    def dispute_action(self,run,account,dispute_id,action,version):
        need(action in {"win","lose"},400,"simulator_unsupported")
        with self.transaction():
            self._run(run)
            scope = self._billing_scope(run,account)
            dispute = self._object(run,"dispute",dispute_id,scope)
            need(type(version) is int and version == dispute["version"],409,"simulator_version")
            need(dispute["status"] == "needs_response",409,"simulator_state")
            dispute["status"] = "won" if action == "win" else "lost"
            dispute = self._put(run,scope,"dispute",dispute)
            if action == "win" and dispute.get("local_balance_debit"):
                self._balance_entry(run,scope,dispute_id,dispute["amount"],0,int(self.clock()),status="available",category="dispute_reversal",source_type=dispute.get("local_source_type","card"))
            self._event(run,scope,"charge.dispute.closed",dispute)
            return {"provider_version":dispute["version"],"status":dispute["status"]}

    def _advance_payment(self, run, account, doc, action, key=None):
        self._account_ready(run,account)
        row = self._run(run)
        state = json.loads(row["state"])
        if action in {"confirm", "decline"}:
            need(doc["status"] == "requires_payment_method",409,"simulator_state")
            outcome = ("declined" if action == "decline" or state.get("decline_next")
                       else state.get("next_confirmation", "requires_capture"))
            if state.get("decline_next") or state.get("next_confirmation"):
                state["decline_next"] = False
                state.pop("next_confirmation", None)
                self.connection.execute("UPDATE scenario_runs SET state=?,state_version=state_version+1 WHERE simulation_run_id=?",(wire.dumps(state).decode(),run))
            doc["status"] = "requires_payment_method" if outcome == "declined" else outcome
            doc["last_payment_error"] = {"type":"card_error","code":"card_declined"} if outcome == "declined" else None
            if outcome == "requires_action":
                doc["next_action"] = {"type":"use_stripe_sdk"}
            event = {"declined":"payment_intent.payment_failed", "requires_action":"payment_intent.requires_action",
                     "processing":"payment_intent.processing", "requires_capture":"payment_intent.amount_capturable_updated"}[outcome]
        else:
            expected = "processing" if action == "complete_processing" else "requires_action"
            need(doc["status"] == expected,409,"simulator_state")
            failed = action == "fail_authentication"
            doc["status"] = "requires_payment_method" if failed else "requires_capture"
            doc.pop("next_action", None)
            doc["last_payment_error"] = {"type":"card_error","code":"authentication_failed"} if failed else None
            event = "payment_intent.payment_failed" if failed else "payment_intent.amount_capturable_updated"
        if doc["status"] == "requires_capture" and doc["capture_method"] == "automatic":
            return self._capture_payment(run,account,doc,key)
        doc = self._put(run,account,"payment_intent",doc)
        self._event(run,account,event,doc,key)
        return doc

    def component_action(self, run, account, purpose, resource, action, version):
        with self.transaction():
            row = self._run(run)
            need(action in scenarios.COMPONENT_ACTIONS.get(purpose,()),400,"simulator_unsupported")
            kind = "payment_intent" if purpose == "card_payment" else "account"
            doc = self._object(run,kind,resource,account)
            need(type(version) is int and doc["version"] == version,409,"simulator_version")
            if action == "exit":
                return {"provider_version":doc["version"],"closed":True}
            if purpose == "onboarding":
                ready = action == "requirements_complete"
                doc = wire.account_projection(account,doc["metadata"],ready,doc["created"])
                doc = self._put(run,account,"account",doc)
                self._event(run,account,"account.updated",doc)
            elif purpose in {"account_settings","payout_settings"}:
                doc["synthetic_settings"] = dict(doc.get("synthetic_settings",{}),last_action=action)
                doc = self._put(run,account,"account",doc)
                self._event(run,account,"account.updated",doc)
            else:
                doc = self._advance_payment(run,account,doc,action)
            return {"provider_version":doc["version"],"closed":False}

    def apply_idempotent(self, run, account, method, target, key, raw, version, operation):
        """Called under the same SQLite transaction as resources and the outbox."""
        need(isinstance(key,str) and wire.TOKEN.fullmatch(key),400,"simulator_idempotency")
        scope = account or self.platform_account
        digest = hashlib.sha256(wire.dumps([method,target,version,account]) + b"\x00" + raw).hexdigest()
        # Query bytes belong to the fingerprint, not a separate key namespace.
        args = (run,scope,method,urlsplit(target).path,key)
        prior = self.connection.execute("SELECT * FROM idempotency WHERE simulation_run_id=? AND account_id=? AND method=? AND path=? AND key=?",args).fetchone()
        if prior:
            need(prior["request_sha256"] == digest,409,"simulator_idempotency")
            return Result(prior["status"],json.loads(prior["response_projection"]))
        result = operation()
        need(self.connection.execute("SELECT count(*) FROM idempotency").fetchone()[0] < wire.MAX_OBJECTS * 4,429,"simulator_limit")
        self.connection.execute("INSERT INTO idempotency VALUES(?,?,?,?,?,?,?,?,?)",
            (*args,digest,result.body.get("id"),result.status,wire.dumps(result.body).decode()))
        row = self._run(run)
        state = json.loads(row["state"])
        drop = state["drop_next"]
        if drop:
            state["drop_next"] = False
            self.connection.execute("UPDATE scenario_runs SET state=?,state_version=state_version+1 WHERE simulation_run_id=?",
                                    (wire.dumps(state).decode(),run))
        return Result(result.status,result.body,drop)

    def request(self, run, account, method, target, raw=b"", api_version=_DEFAULT_API_VERSION, key=None):
        if api_version is _DEFAULT_API_VERSION:
            api_version = self.api_version
        try:
            def checked_request():
                need(api_version == self.api_version)
                return self._request(run,account,method,target,raw,key)
            need(isinstance(raw,bytes) and len(raw) <= wire.MAX_BODY,413,"simulator_limit")
            with self.transaction():
                self._run(run)
                # Replay fingerprint checks precede form decoding, including malformed drift.
                if method == "POST" and target != "/v1/account_sessions":
                    return self.apply_idempotent(run,account,method,target,key,raw,api_version,
                        checked_request)
                need(key is None)
                return checked_request()
        except SimulatorError as error:
            return error.result()
        except (sqlite3.Error,OSError):
            return SimulatorError(503,"simulator_state").result()

    def _request(self, run, account, method, target, raw, key):
        kind, args, form = wire.route(method,target,raw)
        if kind.startswith("billing_"):
            scope = self._billing_scope(run,account)
            get_kinds = {"billing_customer_get":"customer", "billing_product_get":"product",
                         "billing_price_get":"price", "billing_subscription_get":"subscription",
                         "billing_session_get":"checkout_session", "billing_invoice_get":"invoice",
                         "billing_portal_configuration_get":"portal_configuration", "billing_payout_get":"payout",
                         "billing_dispute_get":"dispute", "billing_mandate_get":"mandate",
                         "billing_promotion_get":"promotion_code"}
            list_kinds = {"billing_subscription_list":"subscription", "billing_invoice_list":"invoice",
                          "billing_invoice_payment_list":"invoice_payment", "billing_charge_list":"charge",
                          "billing_dispute_list":"dispute", "billing_line_list":"invoice_line",
                          "billing_session_list":"checkout_session", "billing_portal_configuration_list":"portal_configuration",
                          "billing_payout_list":"payout", "billing_promotion_list":"promotion_code"}
            if kind in get_kinds:
                doc = self._public(run,self._object(run,get_kinds[kind],args[0],scope))
                if form.get("expand[]") == "discounts":
                    doc["discounts"] = [self._public(run,self._object(run,"discount",discount,scope)) for discount in doc.get("discounts",[])]
                return Result(200,doc)
            return Result(200,self._billing_list(run,scope,list_kinds[kind],form,urlsplit(target).path,
                                                args[0] if kind == "billing_line_list" else None))
        if kind == "event_v2_get":
            need(account is None,403,"simulator_resource")
            row = self.connection.execute("SELECT payload FROM webhook_outbox WHERE simulation_run_id=? AND event_id=?",(run,args[0])).fetchone()
            need(row is not None,403,"simulator_resource")
            event = json.loads(bytes(row[0]))
            need(event.get("object") == "v2.core.event",403,"simulator_resource")
            return Result(200,event)
        elif kind == "tax_settings_get":
            scope = self._billing_scope(run,account)
            return Result(200,{"object":"tax.settings","livemode":False,
                "status":"active" if self._tax_settings(run,scope) else "pending"})
        elif kind == "account_v2_create":
            need(self.api_version != wire.LEGACY_API_VERSION,400,"simulator_unsupported")
            need(account is None,403,"simulator_resource")
            wire.closed(form,{"identity","configuration"},{"contact_email","display_name","include"})
            identity = wire.closed(form["identity"],{"country","entity_type"})
            need(identity["country"] == "us" and isinstance(identity["entity_type"],str)
                 and identity["entity_type"] in {"company","individual"})
            config = wire.closed(form["configuration"],{"merchant"})
            merchant = wire.closed(config["merchant"],{"capabilities"})
            caps = wire.closed(merchant["capabilities"],{"card_payments"})
            card = wire.closed(caps["card_payments"],{"requested"})
            need(card["requested"] is True)
            email = form.get("contact_email")
            need(email is None or (isinstance(email,str) and re.fullmatch(r"[A-Za-z0-9._+-]{1,64}@example\.test",email) is not None))
            name = form.get("display_name")
            need(name is None or (isinstance(name,str) and re.fullmatch(r"Synthetic(?:[ A-Za-z0-9_-]{0,55})",name) is not None))
            include = form.get("include",[])
            need(isinstance(include,list) and len(include) <= 3 and all(isinstance(x,str) and x in {"identity","configuration.merchant","requirements"} for x in include))
            need(len(set(include)) == len(include))
            account_id = new_id("acct")
            now = datetime.fromtimestamp(self.clock(),timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
            doc = self._put(run,account_id,"account_v2",{"id":account_id,"object":"v2.core.account","livemode":False,
                "created":now,"contact_email":email,"display_name":name,"applied_configurations":["merchant"],
                "identity":dict(identity),**v2_identity_state(False)})
            self._v2_event(run,account_id,"v2.core.account.created")
            return Result(200,v2_projection(doc,include))
        elif kind == "account_v2_get":
            need(self.api_version != wire.LEGACY_API_VERSION,400,"simulator_unsupported")
            need(account is None,403,"simulator_resource")
            doc = self._object(run,"account_v2",args[0],args[0])
            return Result(200,v2_projection(doc,list(form.values())))
        elif kind == "balance_get":
            scope = self._billing_scope(run,account)
            balance = self._balance(run,scope)
            pending = {source:self._pending_balance(run,scope,source) for source in ("card","bank_account")}
            return Result(200,{"object":"balance","livemode":False,
                "available":[{"amount":balance["available"],"currency":"usd","source_types":dict(balance["local_sources"])}],
                "pending":[{"amount":sum(pending.values()),"currency":"usd","source_types":pending}]})
        elif kind == "balance_transaction_get":
            scope = self._billing_scope(run,account)
            doc = self._object(run,"balance_transaction",args[0],scope)
        elif kind == "payout_create":
            scope = self._billing_scope(run,account)
            if scope != self.platform_account:
                need(self._object(run,"account",scope,scope)["payouts_enabled"],409,"simulator_not_ready")
            fields, meta = wire.metadata(form)
            wire.closed(fields,{"amount","currency"},{"method","source_type"})
            need(fields["currency"] == "usd" and fields.get("method","standard") == "standard" and
                 fields.get("source_type","card") in {"card","bank_account"},400,"simulator_unsupported")
            amount = wire.integer(fields["amount"],1)
            balance = self._balance(run,scope)
            source_type = fields.get("source_type","card")
            need(balance["local_sources"][source_type] >= amount and balance["available"] >= amount,409,"simulator_insufficient_funds")
            self._adjust_available(balance,source_type,-amount)
            self._put(run,scope,"balance",balance)
            doc = self._put(run,scope,"payout",{"id":new_id("po"),"object":"payout","livemode":False,
                "amount":amount,"currency":"usd","status":"pending","automatic":False,"method":"standard",
                "source_type":source_type,"type":"bank_account","destination":None,"balance_transaction":None,
                "arrival_date":None,"failure_code":None,"metadata":meta,"created":int(self.clock())})
            self._event(run,scope,"payout.created",doc,key)
        elif kind == "payout_cancel":
            scope = self._billing_scope(run,account)
            wire.closed(form,set())
            doc = self._transition_payout(run,scope,self._object(run,"payout",args[0],scope),"cancel",key)
        elif kind == "customer_create":
            scope = self._billing_scope(run,account)
            wire.closed(form,{"email"})
            need(re.fullmatch(r"[A-Za-z0-9._+-]{1,64}@example\.test",form["email"]) is not None)
            doc = self._put(run,scope,"customer",{"id":new_id("cus"),"object":"customer","livemode":False,
                "email":form["email"],"created":int(self.clock()),"metadata":{}})
        elif kind == "checkout_create":
            scope = self._billing_scope(run,account)
            fields = dict(form)
            metadata = {}
            for name in list(fields):
                match = re.fullmatch(r"subscription_data\[metadata\]\[([A-Za-z0-9_]{1,64})\]",name)
                if match:
                    value = fields.pop(name)
                    need(len(metadata) < 16 and re.fullmatch(r"[A-Za-z0-9_-]{1,128}",value) is not None)
                    metadata[match[1]] = value
            wire.closed(fields,{"mode","line_items[0][price]","line_items[0][quantity]"},
                {"customer","customer_email","success_url","cancel_url","return_url","ui_mode",
                 "client_reference_id","allow_promotion_codes","expires_at","subscription_data[application_fee_percent]",
                 "automatic_tax[enabled]","tax_id_collection[enabled]","billing_address_collection",
                 "customer_update[address]","customer_update[name]","discounts[0][promotion_code]"})
            need(fields["mode"] == "subscription" and fields["line_items[0][quantity]"] == "1")
            need(fields.get("allow_promotion_codes","false") == "false",400,"simulator_unsupported")
            for field in ("automatic_tax[enabled]","tax_id_collection[enabled]"):
                if field in fields:
                    need(fields[field] in {"true","false"})
            if "billing_address_collection" in fields:
                need(fields["billing_address_collection"] == "required",400,"simulator_unsupported")
            for field in ("customer_update[address]","customer_update[name]"):
                if field in fields:
                    need(fields[field] == "auto" and "customer" in fields,400,"simulator_unsupported")
            tax_settings = self._tax_settings(run,scope) if fields.get("automatic_tax[enabled]") == "true" else None
            need(fields.get("automatic_tax[enabled]") != "true" or tax_settings is not None,400,"simulator_unsupported")
            promotion = None
            if "discounts[0][promotion_code]" in fields:
                promotion = self._object(run,"promotion_code",fields["discounts[0][promotion_code]"],scope)
                need(promotion["active"] and promotion["promotion"]["coupon"]["valid"],409,"simulator_state")
            mode = fields.get("ui_mode","hosted")
            need(mode in {"hosted","elements"},400,"simulator_unsupported")
            need((mode == "hosted" and {"success_url","cancel_url"} <= fields.keys() and "return_url" not in fields) or
                 (mode == "elements" and "return_url" in fields and "success_url" not in fields and "cancel_url" not in fields))
            for key in ("success_url","cancel_url","return_url"):
                if key in fields:
                    need(return_url(fields[key]))
            if "client_reference_id" in fields:
                need(re.fullmatch(r"[A-Za-z0-9_-]{1,64}",fields["client_reference_id"]) is not None)
            need(not ("customer" in fields and "customer_email" in fields))
            if "customer" in fields:
                self._object(run,"customer",fields["customer"],scope)
            if "customer_email" in fields:
                need(re.fullmatch(r"[A-Za-z0-9._+-]{1,64}@example\.test",fields["customer_email"]) is not None)
            fee_percent = fields.get("subscription_data[application_fee_percent]")
            if fee_percent is not None:
                need(scope != self.platform_account and re.fullmatch(r"(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,2})?",fee_percent) is not None)
                need(Decimal(fee_percent) <= 100)
                fee_percent = float(fee_percent)
            price = self._object(run,"price",fields["line_items[0][price]"],scope)
            need(price.get("active") and price.get("type") == "recurring" and price.get("currency") == "usd"
                 and price.get("recurring") in ({"interval":"year","interval_count":1},{"interval":"month","interval_count":1}),400,"simulator_unsupported")
            now = int(self.clock())
            if "expires_at" in fields:
                need(re.fullmatch(r"[1-9][0-9]{0,10}",fields["expires_at"]) is not None)
                expires = int(fields["expires_at"])
                need(now+1800 <= expires <= now+86400)
            else:
                expires = now+86400
            session_id = new_id("cs")
            discount_id = None
            if promotion:
                discount_id = new_id("di")
                self._put(run,scope,"discount",{"id":discount_id,"object":"discount","livemode":False,
                    "promotion_code":promotion["id"],"coupon":promotion["promotion"]["coupon"],
                    "customer":fields.get("customer"),"checkout_session":session_id,"subscription":None,"start":now,"end":None})
            doc = self._put(run,scope,"checkout_session",{"id":session_id,"object":"checkout.session",
                "livemode":False,"mode":"subscription","ui_mode":mode,"status":"open","payment_status":"unpaid",
                "customer":fields.get("customer"),"customer_email":fields.get("customer_email"),"subscription":None,
                "client_reference_id":fields.get("client_reference_id"),"created":now,"expires_at":expires,
                "url":f"https://checkout.local.invalid/{session_id}" if mode == "hosted" else None,
                "automatic_tax":{"enabled":tax_settings is not None,"status":None},
                "tax_id_collection":{"enabled":fields.get("tax_id_collection[enabled]") == "true"},
                "billing_address_collection":fields.get("billing_address_collection","auto"),
                "discounts":[discount_id] if discount_id else [],
                "local_price":price["id"],"local_subscription_metadata":metadata,"local_fee_percent":fee_percent,
                "local_success_url":fields.get("success_url"),"local_cancel_url":fields.get("cancel_url"),
                "local_tax_rate_bps":tax_settings["rate_bps"] if tax_settings else None,
                "local_promotion_percent":promotion["promotion"]["coupon"]["percent_off"] if promotion else 0,
                "local_promotion_duration":promotion["promotion"]["coupon"]["duration"] if promotion else None})
        elif kind == "portal_configuration_create":
            scope = self._billing_scope(run,account)
            fields, meta = wire.metadata(form)
            flags = {"customer_update","invoice_history","payment_method_update","subscription_cancel","subscription_update"}
            required = {f"features[{name}][enabled]" for name in flags} | {
                "features[subscription_cancel][mode]","features[subscription_cancel][proration_behavior]",
                "login_page[enabled]","default_return_url"}
            wire.closed(fields,required)
            need(all(fields[f"features[{name}][enabled]"] in {"true","false"} for name in flags))
            need(fields["features[subscription_cancel][mode]"] == "at_period_end" and
                 fields["features[subscription_cancel][proration_behavior]"] == "none" and
                 fields["login_page[enabled]"] == "false",400,"simulator_unsupported")
            need(return_url(fields["default_return_url"]))
            features = {name:{"enabled":fields[f"features[{name}][enabled]"] == "true"} for name in flags}
            features["subscription_cancel"].update(mode="at_period_end",proration_behavior="none")
            now = int(self.clock())
            doc = self._put(run,scope,"portal_configuration",{"id":new_id("bpc"),"object":"billing_portal.configuration",
                "livemode":False,"active":True,"is_default":False,"created":now,"updated":now,"metadata":meta,
                "default_return_url":fields["default_return_url"],"login_page":{"enabled":False,"url":None},"features":features})
        elif kind == "portal_create":
            scope = self._billing_scope(run,account)
            wire.closed(form,{"customer","return_url"},{"configuration"})
            self._object(run,"customer",form["customer"],scope)
            if "configuration" in form:
                config = self._object(run,"portal_configuration",form["configuration"],scope)
                need(config["active"],409,"simulator_state")
            need(return_url(form["return_url"]))
            portal_id = new_id("bps")
            doc = self._put(run,scope,"portal_session",{"id":portal_id,"object":"billing_portal.session",
                "livemode":False,"customer":form["customer"],"return_url":form["return_url"],"configuration":form.get("configuration"),
                "created":int(self.clock()),"url":f"https://billing.local.invalid/{portal_id}"})
        elif kind == "account_create":
            need(account is None,403,"simulator_resource")
            fields, meta = wire.metadata(form)
            need(fields == wire.ACCOUNT_FORM)
            state = json.loads(self._run(run)["state"])
            doc = self._put(run,new := new_id("acct"),"account",
                            wire.account_projection(new,meta,state["ready"],self.clock()))
            self._event(run,new,"account.updated",doc,key)
        elif kind == "account_get":
            need(account is None,403,"simulator_resource")
            doc = self._object(run,"account",args[0],args[0])
        elif kind == "session":
            need(account is None,403,"simulator_resource")
            purpose, components = wire.session(form)
            self._object(run,"account",form["account"],form["account"])
            return Result(200,{"object":"account_session","account":form["account"],"livemode":False,
                               "expires_at":int(self.clock())+300,"components":components})
        elif kind == "intent_create":
            account = self._billing_scope(run,account)
            self._account_ready(run,account)
            fields, meta = wire.metadata(form)
            required = {"amount", "currency"}
            optional = {"application_fee_amount", "capture_method"}
            if self.api_version == wire.LEGACY_API_VERSION:
                wire.closed(fields, required | {"payment_method_types[]", "capture_method"}, {"application_fee_amount"})
                need(fields["payment_method_types[]"] == "card")
                method = "card"
            else:
                need("payment_method_types[]" not in fields and "payment_method_types" not in fields,
                     400, "payment_method_types_no_longer_supported")
                wire.closed(fields, required, optional | {"allowed_payment_method_types[]"})
                # This profile models a card-only payment-method configuration.
                # Dynamic selection therefore also resolves to card when omitted.
                method = fields.get("allowed_payment_method_types[]", "card")
                need(method in {"card","us_bank_account"},
                     400, "simulator_unsupported")
            amount = wire.integer(fields["amount"],50)
            fee = wire.integer(fields.get("application_fee_amount","0"))
            capture_method = fields.get("capture_method", "automatic")
            need(fields["currency"] == "usd" and capture_method in {"manual", "automatic"} and fee < amount)
            need(method == "card" or capture_method == "automatic",400,"simulator_unsupported")
            need(account != self.platform_account or fee == 0,400,"simulator_unsupported")
            doc = self._put(run,account,"payment_intent",{"id":new_id("pi"),"object":"payment_intent","livemode":False,
                "amount":amount,"amount_received":0,"currency":"usd","capture_method":capture_method,"payment_method_types":[method],
                "application_fee_amount":fee,"metadata":meta,"status":"requires_payment_method","latest_charge":None,
                "last_payment_error":None,"created":int(self.clock())})
            self._event(run,account,"payment_intent.created",doc,key)
        elif kind == "intent_get":
            scope = self._billing_scope(run,account)
            doc = self._object(run,"payment_intent",args[0],scope)
        elif kind == "intent_action":
            account = self._billing_scope(run,account)
            doc = self._object(run,"payment_intent",args[0],account)
            if args[1] == "confirm":
                wire.closed(form,{"payment_method"})
                if doc["payment_method_types"] == ["us_bank_account"]:
                    need(form["payment_method"] in {"pm_usBankAccount_processing","pm_usBankAccount_unverified"},400,"simulator_unsupported")
                    self._account_ready(run,account)
                    need(doc["status"] == "requires_payment_method",409,"simulator_state")
                    unverified = form["payment_method"] == "pm_usBankAccount_unverified"
                    doc.pop("mandate",None)
                    doc.pop("local_verification_attempts",None)
                    doc.pop("next_action",None)
                    doc.update(payment_method=form["payment_method"],status="requires_action" if unverified else "processing",last_payment_error=None)
                    if unverified:
                        mandate = self._put(run,account,"mandate",{"id":new_id("mandate"),"object":"mandate",
                            "livemode":False,"payment_method":form["payment_method"],"status":"pending","type":"multi_use"})
                        doc.update(mandate=mandate["id"],local_verification_attempts=0,
                            next_action={"type":"verify_with_microdeposits","verify_with_microdeposits":{"microdeposit_type":"descriptor_code"}})
                    doc = self._put(run,account,"payment_intent",doc)
                    self._event(run,account,"payment_intent.requires_action" if unverified else "payment_intent.processing",doc,key)
                    return Result(200,self._public(run,doc))
                need(form["payment_method"] == "pm_card_visa",400,"simulator_unsupported")
                doc["payment_method"] = "pm_card_visa"
                doc = self._advance_payment(run,account,doc,"confirm",key)
                if doc["status"] == "requires_payment_method":
                    return Result(402,{"error":{"type":"card_error","code":"card_declined",
                        "message":"The simulated card was declined.","payment_intent":self._public(run,doc)}})
                return Result(200,self._public(run,doc))
            if args[1] == "capture":
                self._account_ready(run,account)
                wire.closed(form,{"amount_to_capture"},{"expand[]"})
                need(form["amount_to_capture"] == str(doc["amount"])
                     and form.get("expand[]","latest_charge") == "latest_charge")
                doc = self._capture_payment(run,account,doc,key)
            else:
                need(form in ({},{"cancellation_reason":"requested_by_customer"}))
                allowed = {"requires_payment_method","requires_capture"}
                if doc["payment_method_types"] == ["us_bank_account"]:
                    allowed |= {"requires_action","processing"}
                need(doc["status"] in allowed,409,"simulator_state")
                doc["status"] = "canceled"
                doc = self._put(run,account,"payment_intent",doc)
                if doc.get("mandate"):
                    mandate = self._object(run,"mandate",doc["mandate"],account)
                    if mandate["status"] != "inactive":
                        mandate["status"] = "inactive"
                        mandate = self._put(run,account,"mandate",mandate)
                        self._event(run,account,"mandate.updated",mandate,key)
                self._event(run,account,"payment_intent.canceled",doc,key)
        elif kind == "bank_verify":
            scope = self._billing_scope(run,account)
            wire.closed(form,{"descriptor_code"})
            doc = self._object(run,"payment_intent",args[0],scope)
            need(doc.get("payment_method_types") == ["us_bank_account"] and doc.get("status") == "requires_action"
                 and doc.get("mandate"),409,"simulator_state")
            mandate = self._object(run,"mandate",doc["mandate"],scope)
            if form["descriptor_code"] != "SM11AA":
                attempts = doc.get("local_verification_attempts",0) + 1
                doc["local_verification_attempts"] = attempts
                if attempts >= 3:
                    doc.update(status="requires_payment_method",last_payment_error={"type":"invalid_request_error",
                        "code":"payment_method_microdeposit_verification_attempts_exceeded"})
                    doc.pop("next_action",None)
                    doc = self._put(run,scope,"payment_intent",doc)
                    mandate["status"] = "inactive"
                    mandate = self._put(run,scope,"mandate",mandate)
                    self._event(run,scope,"mandate.updated",mandate,key)
                    self._event(run,scope,"payment_intent.payment_failed",doc,key)
                else:
                    self._put(run,scope,"payment_intent",doc)
                code = "payment_method_microdeposit_verification_attempts_exceeded" if attempts >= 3 else "payment_method_microdeposit_verification_amounts_mismatch"
                return Result(400,{"error":{"type":"invalid_request_error","code":code,"message":"Synthetic verification failed"}})
            self._account_ready(run,scope)
            doc.update(status="processing",last_payment_error=None)
            doc.pop("next_action",None)
            doc = self._put(run,scope,"payment_intent",doc)
            mandate["status"] = "active"
            mandate = self._put(run,scope,"mandate",mandate)
            self._event(run,scope,"mandate.updated",mandate,key)
            self._event(run,scope,"payment_intent.processing",doc,key)
        elif kind in {"charge_get","refund_get"}:
            scope = self._billing_scope(run,account)
            doc = self._object(run,"charge" if kind == "charge_get" else "refund",args[0],scope)
            if form.get("expand[]") == "balance_transaction" and doc.get("balance_transaction"):
                doc["balance_transaction"] = self._public(run,self._object(run,"balance_transaction",doc["balance_transaction"],scope))
        elif kind == "refund_create":
            account = self._billing_scope(run,account)
            fields, meta = wire.metadata(form)
            wire.closed(fields,{"charge","amount","refund_application_fee"})
            need(fields["refund_application_fee"] == "false")
            charge = self._object(run,"charge",fields["charge"],account)
            self._assert_refundable(run,account,fields["charge"])
            amount = wire.integer(fields["amount"],1)
            prior = [json.loads(x[0]) for x in self.connection.execute(
                "SELECT document FROM provider_objects WHERE simulation_run_id=? AND kind='refund' AND account_id=?",(run,account))]
            reserved = sum(x["amount"] for x in prior if x["charge"] == charge["id"] and x["status"] in {"pending","succeeded"})
            need(charge["captured"] and reserved + amount <= charge["amount_captured"],409,"simulator_conservation")
            pending = json.loads(self._run(run)["state"])["refund_pending"]
            doc = self._put(run,account,"refund",{"id":new_id("re"),"object":"refund","livemode":False,
                "charge":charge["id"],"payment_intent":charge["payment_intent"],"amount":amount,"currency":"usd",
                "status":"pending" if pending else "succeeded","metadata":meta,"balance_transaction":None,"created":int(self.clock())})
            if "customer" in charge:
                doc["customer"] = charge["customer"]
                doc = self._put(run,account,"refund",doc)
            if not pending:
                doc = self._refresh_refunded_charge(run,account,doc)
            self._event(run,account,"refund.created",doc,key)
        elif kind == "fee_get":
            need(account is None,403,"simulator_resource")
            doc = self._object(run,"application_fee",args[0])
        elif kind == "fee_refund_create":
            need(account is None,403,"simulator_resource")
            fee = self._object(run,"application_fee",args[0])
            fields, meta = wire.metadata(form)
            wire.closed(fields,{"amount"})
            amount = wire.integer(fields["amount"],1)
            prior = [json.loads(x[0]) for x in self.connection.execute(
                "SELECT document FROM provider_objects WHERE simulation_run_id=? AND kind='fee_refund'",(run,))]
            need(sum(x["amount"] for x in prior if x["fee"] == fee["id"]) + amount <= fee["amount"],409,"simulator_conservation")
            doc = self._put(run,fee["account"],"fee_refund",{"id":new_id("fr"),"object":"fee_refund",
                "fee":fee["id"],"amount":amount,"currency":"usd","balance_transaction":None,"metadata":meta,"created":int(self.clock())})
            source_type = self._charge_source(self._object(run,"charge",fee["charge"],fee["account"]))
            transaction = self._balance_debit(run,self.platform_account,doc["id"],amount,"application_fee_refund",source_type)
            self._balance_entry(run,fee["account"],doc["id"],amount,0,int(self.clock()),status="available",category="application_fee_refund",source_type=source_type)
            if self.api_version != wire.LEGACY_API_VERSION:
                doc["balance_transaction"] = transaction
                doc = self._put(run,fee["account"],"fee_refund",doc)
            self._event(run,fee["account"],"application_fee.refunded",fee,key)
        elif kind == "fee_refund_get":
            need(account is None,403,"simulator_resource")
            fee = self._object(run,"application_fee",args[0])
            doc = self._object(run,"fee_refund",args[1],fee["account"])
            need(doc["fee"] == fee["id"],403,"simulator_resource")
        else:
            raise SimulatorError(400,"simulator_unsupported")
        return Result(200,self._public(run,doc))
