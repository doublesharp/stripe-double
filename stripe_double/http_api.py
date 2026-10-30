"""Synthetic-only loopback HTTP process with explicit owned shutdown."""
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import hmac
import ipaddress
import json
import re
import secrets
import socket
import sqlite3
import threading
from . import wire
from .capabilities import CapabilityStore
from .hosted import HostedRegistry, browser
from .webhooks import Outbox
from .wire import SimulatorError, Result, need


@dataclass(repr=False, frozen=True)
class Credentials:
    api_key: str = field(default_factory=lambda:"sk_test_LocalSim"+secrets.token_hex(16))
    public_key: str = field(default_factory=lambda:"pk_test_LocalSim"+secrets.token_hex(16))
    admin_token: str = field(default_factory=lambda:"sim_admin_"+secrets.token_urlsafe(32))
    webhook_secret: str = field(default_factory=lambda:"whsec_LocalSim"+secrets.token_hex(32))

    def __post_init__(self):
        for value, pattern in [
            (self.api_key,r"sk_test_LocalSim[0-9a-f]{32}"),
            (self.public_key,r"pk_test_LocalSim[0-9a-f]{32}"),
            (self.admin_token,r"sim_admin_[A-Za-z0-9_-]{43}"),
            (self.webhook_secret,r"whsec_LocalSim[0-9a-f]{64}"),
        ]:
            need(isinstance(value,str) and re.fullmatch(pattern,value) is not None)

    def __repr__(self):
        return "Credentials([REDACTED])"


class _BoundedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False
    request_queue_size = 8

    def __init__(self, *args):
        self._slots = threading.BoundedSemaphore(8)
        self._clients = set()
        self._clients_lock = threading.Lock()
        super().__init__(*args)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._clients_lock:
            self._clients.add(request)
        try:
            super().process_request(request,client_address)
        except BaseException:
            with self._clients_lock:
                self._clients.discard(request)
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request,client_address)
        finally:
            with self._clients_lock:
                self._clients.discard(request)
            self._slots.release()

    def close_clients(self):
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()

    def handle_error(self,request,client_address):
        # Request bodies, provider objects, tokens and raw errors are never logged.
        pass


class SimulatorServer:
    def __init__(self, store, clock=None, callback=None, host="127.0.0.1", port=0, credentials=None, automatic_webhooks=False,
                 webhook_max_attempts=1, webhook_retry_delay=1):
        address = ipaddress.ip_address(host)
        need(address.is_loopback and str(address) == host)
        need(type(port) is int and 0 <= port <= 65535)
        self.store = store
        self.clock = clock or store.clock
        self.credentials = credentials or Credentials()
        need(isinstance(self.credentials,Credentials))
        self.capabilities = CapabilityStore(self.clock)
        self.hosted = HostedRegistry(self.clock)
        self._control_lock = threading.RLock()
        # Validate callback before allocating any listener.
        self.outbox = Outbox(store,self.credentials.webhook_secret,self.clock,callback,webhook_max_attempts,webhook_retry_delay)
        need(type(automatic_webhooks) is bool and (not automatic_webhooks or self.outbox.callback is not None))
        self._automatic_stop = threading.Event()
        self._automatic_ready = threading.Event()
        self._automatic_thread = (threading.Thread(target=self._automatic_delivery,name="stripe-webhook-delivery")
                                  if automatic_webhooks else None)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            server_version = "LocalStripeSimulator/1"
            sys_version = ""

            def setup(self):
                self.request.settimeout(1)
                super().setup()

            def log_message(self,*args):
                pass

            def send_error(self,code,message=None,explain=None):
                self._respond(SimulatorError(code,"simulator_invalid").result())

            def _respond(self,result,run=None):
                data = wire.dumps(result.body)
                self.close_connection = True
                try:
                    self.send_response(result.status)
                    self.send_header("Content-Type","application/json")
                    self.send_header("Content-Length",str(len(data)))
                    self.send_header("Cache-Control","no-store")
                    self.send_header("Connection","close")
                    self.send_header("X-Local-Simulator-Instance",outer.store.instance_id)
                    if run:
                        self.send_header("X-Local-Simulator-Run",run)
                    self.end_headers()
                    self.wfile.write(data)
                except (OSError,ConnectionError):
                    self.close_connection = True

            def _handle(self):
                run = None
                try:
                    self.close_connection = True
                    need(self.command in {"GET","POST"},400,"simulator_unsupported")
                    need(len(self.path) <= 2048 and self.path.startswith("/") and not self.path.startswith("//"))
                    names = [key.lower() for key in self.headers]
                    need(len(names) == len(set(names)) and len(names) <= 64)
                    local_host = f"[{outer.address[0]}]" if ":" in outer.address[0] else outer.address[0]
                    origin = f"http://{local_host}:{outer.address[1]}"
                    need(self.headers.get("Host") == f"{local_host}:{outer.address[1]}")
                    length = self.headers.get("Content-Length","0")
                    need(re.fullmatch(r"0|[1-9][0-9]{0,5}",length) is not None)
                    length = int(length)
                    need(length <= wire.MAX_BODY,413,"simulator_limit")
                    need(self.command != "POST" or "Content-Length" in self.headers)
                    need(self.command != "GET" or length == 0)
                    if self.path.startswith("/hosted/") or self.path.split("?",1)[0] == "/oauth/authorize":
                        status, data, location, form_origins = browser(self,outer,origin)
                        self.send_response(status)
                        self.send_header("Content-Length",str(len(data)))
                        self.send_header("Cache-Control","no-store")
                        self.send_header("Referrer-Policy","strict-origin")
                        self.send_header("Content-Security-Policy","default-src 'none'; style-src 'unsafe-inline'; form-action 'self' "+form_origins+"; frame-ancestors 'none'")
                        self.send_header("X-Content-Type-Options","nosniff")
                        if location:
                            self.send_header("Location",location)
                        else:
                            self.send_header("Content-Type","text/html; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    allowed = {"host","authorization","stripe-version","stripe-account","idempotency-key",
                               "x-local-simulator-instance","x-local-simulator-run","content-type","content-length",
                               "connection","accept","accept-encoding","accept-language","sec-fetch-mode",
                               "user-agent","x-stripe-client-user-agent","x-stripe-client-telemetry"}
                    need(set(names) <= allowed and len(names) == len(set(names)) and len(names) <= 16)
                    need(self.headers.get("X-Local-Simulator-Instance") == outer.store.instance_id,403,"simulator_instance")
                    internal = self.path.startswith("/__simulator/")
                    admin = self.path.startswith("/__simulator/v1/runs")
                    expected = outer.credentials.admin_token if admin else outer.credentials.api_key
                    authorization = self.headers.get("Authorization","")
                    grant = None
                    if not hmac.compare_digest(authorization,"Bearer "+expected):
                        need(not internal and authorization.startswith("Bearer "),403,"simulator_auth")
                        grant = outer.hosted.get(authorization[7:],"access")
                    content_type = self.headers.get("Content-Type")
                    if self.command == "POST":
                        need(content_type == ("application/json" if internal or self.path.startswith('/v2/') else "application/x-www-form-urlencoded"))
                    raw = self.rfile.read(length)
                    need(len(raw) == length)
                    run = self.headers.get("X-Local-Simulator-Run")
                    if self.path != "/__simulator/v1/runs":
                        outer.store.run(run,status=403)
                    account = self.headers.get("Stripe-Account")
                    if grant is not None:
                        need(grant["run"] == run and account in (None,grant["account"]),403,"simulator_auth")
                        need(self.path.startswith(("/v1/customers", "/v1/subscriptions", "/v1/invoices", "/v1/payment_intents", "/v1/charges", "/v1/refunds", "/v1/balance")) or self.path == "/v1/accounts/"+grant["account"],403,"simulator_auth")
                        account = None if self.path == "/v1/accounts/"+grant["account"] else grant["account"]
                    if internal:
                        need(self.headers.get("Stripe-Version") == outer.store.api_version)
                        need(self.command == "POST",400,"simulator_unsupported")
                        body = wire.json_object(raw)
                        result = outer._internal(self.path,run,body)
                    else:
                        if self.path == "/oauth/token":
                            need(grant is None and self.command == "POST" and self.headers.get("Stripe-Version") == outer.store.api_version)
                            fields = wire.pairs(raw)
                            wire.closed(fields,{"grant_type","code"})
                            need(fields["grant_type"] == "authorization_code")
                            with outer._control_lock:
                                try:
                                    code = outer.hosted.get(fields["code"],"code")
                                    need(code["run"] == run,400,"simulator_auth")
                                except SimulatorError:
                                    raise SimulatorError(400,"simulator_auth")
                                outer.hosted.remove(fields["code"])
                                access = outer.hosted.issue("access",run,code["account"],lifetime=3600)
                                result = Result(200,{"access_token":access,"token_type":"bearer","scope":"read_write","stripe_user_id":code["account"],"livemode":False})
                        else:
                            result = outer._provider_request(run,account,self.command,self.path,raw,
                                self.headers.get("Stripe-Version"),self.headers.get("Idempotency-Key"),origin)
                        if result.status == 200 and self.command == "POST" and self.path == "/v1/checkout/sessions" and result.body.get("ui_mode") == "elements":
                            doc = dict(result.body)
                            # A synthetic placeholder, never persisted or usable by Stripe.js.
                            doc["client_secret"] = doc["id"] + "_secret_" + secrets.token_urlsafe(32)
                            result = Result(result.status,doc,result.drop)
                        elif result.status == 200 and self.path == "/v1/account_sessions":
                            need("Idempotency-Key" not in self.headers)
                            doc = dict(result.body)
                            component = next(iter(doc["components"]))
                            purpose = next(k for k,v in wire.PURPOSES.items() if v == component)
                            secret, lease = outer.capabilities.issue(run,doc["account"],purpose,doc["account"])
                            doc["client_secret"] = secret
                            result = Result(200,doc)
                        elif result.status == 200 and self.command == "GET" and self.path.startswith("/v1/payment_intents/"):
                            doc = dict(result.body)
                            if doc.get("payment_method_types") == ["card"] and doc.get("status") in {"requires_payment_method", "requires_action", "requires_capture"}:
                                if account is None:
                                    account = outer.store.platform_account
                                secret, lease = outer.capabilities.issue(run,account,"card_payment",doc["id"],doc["id"]+"_secret_")
                                doc["client_secret"] = secret
                            else:
                                # Reconciliation polling must not mint and exhaust
                                # volatile UI capabilities for terminal/processing states.
                                doc["client_secret"] = None
                            result = Result(200,doc)
                    if result.drop:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    self._respond(result,run)
                except SimulatorError as error:
                    self._respond(error.result())
                except (TimeoutError,OSError):
                    self.close_connection = True

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_DELETE = _handle
            do_PATCH = _handle

        server_class = _BoundedServer
        if address.version == 6:
            class IPv6Server(_BoundedServer):
                address_family = socket.AF_INET6
            server_class = IPv6Server
        self.http = server_class((host,port),Handler)
        self.address = self.http.server_address[:2]
        self.thread = threading.Thread(target=self.http.serve_forever,kwargs={"poll_interval":.05},name="stripe-simulator-listener")
        self._closed = False
        self._close_lock = threading.Lock()

    def __repr__(self):
        return "SimulatorServer([REDACTED])"

    def _provider_request(self,run,account,method,path,raw,version,key,origin):
        with self._control_lock:
            if method == "POST" and path in {"/v1/checkout/sessions","/v1/billing_portal/sessions"}:
                self.hosted.admit()
            result = self.store.request(run,account,method,path,raw,version,key)
            if result.status != 200:
                return result
            def hydrate(value):
                doc = dict(value)
                purpose = "checkout" if doc.get("object") == "checkout.session" else "portal" if doc.get("object") == "billing_portal.session" else None
                if purpose == "portal" or (purpose == "checkout" and doc.get("ui_mode") == "hosted" and doc.get("status") == "open"):
                    token = self.hosted.issue(purpose,run,account or self.store.platform_account,doc["id"],stable=True)
                    doc["url"] = origin+"/hosted/"+purpose+"/"+token
                return doc
            doc = hydrate(result.body)
            if method == "GET" and path.split("?",1)[0] == "/v1/checkout/sessions" and doc.get("object") == "list":
                doc["data"] = [hydrate(item) for item in doc["data"]]
            return Result(result.status,doc,result.drop)

    @property
    def active_connections(self):
        with self.http._clients_lock:
            return len(self.http._clients)

    def start(self):
        need(not self._closed and self.thread.ident is None,409,"simulator_lifecycle")
        self.thread.start()
        if self._automatic_thread is not None:
            self._automatic_thread.start()
        return self

    def _automatic_delivery(self):
        while not self._automatic_stop.is_set():
            if not self._automatic_ready.wait(.1):
                continue
            try:
                with self.outbox.admit_pump() as pump:
                    with self._control_lock:
                        event = None if self._automatic_stop.is_set() else self.store.reserve_automatic_event(self.outbox.max_attempts)
                    if event is not None:
                        pump(event["simulation_run_id"],event,automatic=True)
                        continue
            except (SimulatorError,sqlite3.Error,OSError):
                pass
            self._automatic_stop.wait(.1)

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._automatic_stop.set()
            self._automatic_ready.set()
            if self._automatic_thread is not None and self._automatic_thread.ident is not None:
                self._automatic_thread.join(3)
                need(not self._automatic_thread.is_alive(),503,"simulator_lifecycle")
            if self.thread.is_alive():
                self.http.shutdown()
            self.http.close_clients()
            self.http.server_close()
            if self.thread.ident is not None:
                self.thread.join(2)
                need(not self.thread.is_alive(),503,"simulator_lifecycle")
            self.capabilities.close()
            self.hosted.close()

    @staticmethod
    def _run_public(row):
        return {key:row[key] for key in ["simulation_run_id","scenario_id","state_version","retired"]}

    def _internal(self,path,run,body):
        # Callback/slot admission must precede the durable version advance.
        # Keep network delivery outside the component/expiry lock.
        if path == f"/__simulator/v1/runs/{run}/actions" and body.get("action") == "deliver_next_event":
            with self.outbox.admit_pump() as pump:
                with self._control_lock:
                    result = self._dispatch_internal(path,run,body)
                value = dict(result.body)
                value["delivery"] = pump(run)
                return Result(result.status,value)
        if path == f"/__simulator/v1/runs/{run}/events/deliver":
            wire.closed(body,{"delivery_id","expected_state_version"})
            need(isinstance(body["delivery_id"],str) and type(body["expected_state_version"]) is int)
            # Reserve the selected event and version atomically before transport;
            # a lost reply cannot make an automatic retry safe.
            with self.outbox.admit_pump() as pump:
                with self._control_lock:
                    row, reserved = self.store.reserve_selected_event(run,body["delivery_id"],body["expected_state_version"])
                value = self._run_public(row)
                value["delivery"] = pump(run,reserved)
                return Result(200,value)
        # Expire/reset cannot acknowledge while an admitted action still owns
        # a lease.
        with self._control_lock:
            return self._dispatch_internal(path,run,body)

    def _dispatch_internal(self,path,run,body):
        for key,value in body.items():
            if key.startswith("expected_") or key == "after_ordinal":
                need(type(value) is int)
            elif key == "metadata":
                need(isinstance(value,dict))
            else:
                need(isinstance(value,str) and len(value) <= 512)
        if path == "/__simulator/v1/runs":
            wire.closed(body,{"scenario_id"})
            return Result(200,self._run_public(self.store.create_run(body["scenario_id"])))
        if path == f"/__simulator/v1/runs/{run}/inspect":
            wire.closed(body,set())
            return Result(200,self._run_public(self.store.run(run)))
        if path == f"/__simulator/v1/runs/{run}/webhooks/start":
            wire.closed(body,set())
            need(self._automatic_thread is not None and not self._closed,409,"simulator_callback")
            self._automatic_ready.set()
            return Result(200,{"automatic_webhooks":"active"})
        if path == f"/__simulator/v1/runs/{run}/events/list":
            wire.closed(body,set(),{"after_ordinal"})
            return Result(200,self.store.delivery_page(run,body.get("after_ordinal",0)))
        if path == f"/__simulator/v1/runs/{run}/resources/inspect":
            wire.closed(body,{"kind","resource_id"},{"account"})
            need(body["kind"] in {"account","payment_intent","charge","refund","application_fee","fee_refund",
                                  "customer","product","price","subscription","invoice","checkout_session","payout","account_v2","dispute","mandate"})
            obj = self.store.object(run,body["kind"],body["resource_id"],body.get("account"))
            version = obj.pop("version")
            return Result(200,{"provider_version":version,"object":obj})
        if path == f"/__simulator/v1/runs/{run}/billing/subscriptions/seed":
            wire.closed(body,{"amount","expected_state_version"},{"account","client_reference_id","metadata","interval"})
            return Result(200,self.store.seed_subscription(run,body.get("account"),body.get("client_reference_id"),
                                                          body.get("metadata",{}),body["amount"],body["expected_state_version"],body.get("interval","year")))
        if path == f"/__simulator/v1/runs/{run}/billing/promotions/seed":
            wire.closed(body,{"code","percent_off","duration","expected_state_version"},{"account"})
            return Result(200,self.store.seed_promotion(run,body.get("account"),body["code"],body["percent_off"],body["duration"],body["expected_state_version"]))
        if path == f"/__simulator/v1/runs/{run}/balances/fund":
            wire.closed(body,{"amount","expected_state_version"},{"account"})
            return Result(200,self.store.fund_balance(run,body.get("account"),body["amount"],body["expected_state_version"]))
        if path == f"/__simulator/v1/runs/{run}/balances/configure":
            wire.closed(body,{"fee_bps","fee_flat","settlement_delay","expected_state_version"},{"account"})
            return Result(200,self.store.configure_balance(run,body.get("account"),body["fee_bps"],body["fee_flat"],body["settlement_delay"],body["expected_state_version"]))
        if path == f"/__simulator/v1/runs/{run}/balances/settle":
            wire.closed(body,{"expected_state_version"},{"account"})
            return Result(200,self.store.settle_balances(run,body.get("account"),body["expected_state_version"]))
        if path == f"/__simulator/v1/runs/{run}/tax/settings/configure":
            wire.closed(body,{"rate_bps","expected_state_version"},{"account"})
            return Result(200,self.store.configure_tax(run,body.get("account"),body["rate_bps"],body["expected_state_version"]))
        match = re.fullmatch(r"/__simulator/v1/runs/([0-9a-f-]{36})/accounts_v2/(acct_LocalSim[0-9a-f]{32})/identity",path)
        if match:
            need(match[1] == run,403,"simulator_run")
            wire.closed(body,{"action","expected_provider_version"})
            return Result(200,self.store.v2_identity_action(run,match[2],body["action"],body["expected_provider_version"]))
        match = re.fullmatch(r"/__simulator/v1/runs/([0-9a-f-]{36})/payment_intents/(pi_LocalSim[0-9a-f]{32})/bank_actions",path)
        if match:
            need(match[1] == run,403,"simulator_run")
            wire.closed(body,{"action","expected_provider_version"},{"account"})
            return Result(200,self.store.bank_payment_action(run,body.get("account"),match[2],body["action"],body["expected_provider_version"]))
        match = re.fullmatch(r"/__simulator/v1/runs/([0-9a-f-]{36})/charges/(ch_LocalSim[0-9a-f]{32})/disputes",path)
        if match:
            need(match[1] == run,403,"simulator_run")
            wire.closed(body,{"expected_provider_version"},{"account"})
            return Result(200,self.store.create_dispute(run,body.get("account"),match[2],body["expected_provider_version"]))
        match = re.fullmatch(r"/__simulator/v1/runs/([0-9a-f-]{36})/disputes/(dp_LocalSim[0-9a-f]{32})/actions",path)
        if match:
            need(match[1] == run,403,"simulator_run")
            wire.closed(body,{"action","expected_provider_version"},{"account"})
            return Result(200,self.store.dispute_action(run,body.get("account"),match[2],body["action"],body["expected_provider_version"]))
        match = re.fullmatch(r"/__simulator/v1/runs/([0-9a-f-]{36})/payouts/(po_LocalSim[0-9a-f]{32})/actions",path)
        if match:
            need(match[1] == run,403,"simulator_run")
            wire.closed(body,{"action","expected_provider_version"},{"account"})
            return Result(200,self.store.payout_action(run,body.get("account"),match[2],body["action"],body["expected_provider_version"]))
        match = re.fullmatch(r"/__simulator/v1/runs/([0-9a-f-]{36})/billing/subscriptions/(sub_LocalSim[0-9a-f]{32})/actions",path)
        if match:
            need(match[1] == run,403,"simulator_run")
            wire.closed(body,{"action","expected_provider_version"},{"account"})
            return Result(200,self.store.subscription_action(run,body.get("account"),match[2],body["action"],body["expected_provider_version"]))
        match = re.fullmatch(r"/__simulator/v1/runs/([0-9a-f-]{36})/checkout/sessions/(cs_LocalSim[0-9a-f]{32})/actions",path)
        if match:
            need(match[1] == run,403,"simulator_run")
            wire.closed(body,{"action","expected_provider_version"},{"account"})
            return Result(200,self.store.checkout_action(run,body.get("account"),match[2],body["action"],body["expected_provider_version"]))
        match = re.fullmatch(r"/__simulator/v1/runs/([0-9a-f-]{36})/refunds/(re_LocalSim[0-9a-f]{32})/actions",path)
        if match:
            need(match[1] == run,403,"simulator_run")
            wire.closed(body,{"action","expected_provider_version"},{"account"})
            return Result(200,self.store.refund_action(run,body.get("account"),match[2],body["action"],body["expected_provider_version"]))
        match = re.fullmatch(r"/__simulator/v1/runs/([0-9a-f-]{36})/accounts/(acct_LocalSim[0-9a-f]{32})/readiness",path)
        if match:
            need(match[1] == run,403,"simulator_run")
            wire.closed(body,{"action","expected_provider_version"})
            return Result(200,self.store.account_readiness(run,match[2],body["action"],body["expected_provider_version"]))
        match = re.fullmatch(r"/__simulator/v1/runs/([0-9a-f-]{36})/(actions|reset)",path)
        if match:
            need(match[1] == run,403,"simulator_run")
            if match[2] == "reset":
                wire.closed(body,{"expected_state_version"})
                # While this lock is held, an admitted delivery either already
                # owns the outbox or cannot reserve until reset has finished.
                need(not self.outbox.is_busy(),409,"simulator_delivery_busy")
                row = self.store.reset_run(run,body["expected_state_version"])
                self.capabilities.expire_run(run)
                self.hosted.expire_run(run)
            else:
                wire.closed(body,{"action","expected_state_version"})
                row = self.store.scenario_action(run,body["action"],body["expected_state_version"])
                if body["action"] == "expire_sessions":
                    self.capabilities.expire_run(run)
                    self.hosted.expire_run(run)
            return Result(200,self._run_public(row))
        if path == "/__simulator/v1/components/exchange":
            wire.closed(body,{"client_secret","account","purpose"})
            need(body["purpose"] in wire.PURPOSES)
            token, lease = self.capabilities.exchange(body["client_secret"],run,body["account"],body["purpose"])
            kind = "payment_intent" if lease.purpose == "card_payment" else "account"
            current = self.store.object(run,kind,lease.resource,lease.account)
            return Result(200,{"control_capability":token,"purpose":lease.purpose,"expires_at_ms":int(lease.expires*1000),
                               "provider_version":current["version"]})
        if path == "/__simulator/v1/components/actions":
            wire.closed(body,{"control_capability","account","purpose","action","expected_provider_version"})
            lease = self.capabilities.consume(body["control_capability"],run,body["account"],body["purpose"])
            result = self.store.component_action(run,lease.account,lease.purpose,lease.resource,body["action"],body["expected_provider_version"])
            if result["closed"]:
                self.capabilities.consume(body["control_capability"],run,lease.account,lease.purpose,retire=True)
            return Result(200,result)
        raise SimulatorError(400,"simulator_unsupported")
