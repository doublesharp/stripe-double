"""Explicit, bounded delivery to one startup-pinned numeric loopback callback."""
from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import hmac
import http.client
import ipaddress
import socket
import threading
from urllib.parse import urlsplit
from .wire import MAX_BODY, need


@dataclass(frozen=True)
class Callback:
    host: str
    port: int
    path: str

    @classmethod
    def parse(cls, value):
        need(isinstance(value,str) and value.isascii()
             and all(32 < ord(c) < 127 for c in value))
        parsed = urlsplit(value)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError as error:
            raise ValueError("callback must use a literal loopback address") from error
        need(parsed.scheme == "http" and address.is_loopback and port is not None
             and 1 <= port <= 65535 and parsed.username is None and parsed.password is None
             and not parsed.query and not parsed.fragment and "?" not in value and "#" not in value
             and parsed.path.startswith("/") and "%" not in parsed.path and "//" not in parsed.path
             and len(parsed.path) <= 1024)
        expected_host = f"[{address}]" if address.version == 6 else str(address)
        need(parsed.netloc == f"{expected_host}:{port}")
        return cls(str(address),port,parsed.path)


class Outbox:
    def __init__(self, store, secret, clock, callback=None, max_attempts=1, retry_delay=1):
        self.store, self._secret, self.clock = store, secret, clock
        need(type(max_attempts) is int and 1 <= max_attempts <= 10 and type(retry_delay) is int and 1 <= retry_delay <= 3600)
        self.max_attempts, self.retry_delay = max_attempts, retry_delay
        self.callback = Callback.parse(callback) if callback is not None else None
        self.attempts = 0
        self._lock = threading.Lock()

    def __repr__(self):
        return "Outbox([REDACTED])"

    @contextmanager
    def admit_pump(self):
        """Reserve delivery before a caller commits its control transition."""
        need(self.callback is not None,409,"simulator_callback")
        need(self._lock.acquire(blocking=False),409,"simulator_delivery_busy")
        try:
            yield self._pump
        finally:
            self._lock.release()

    def is_busy(self):
        return self._lock.locked()

    def pump_once(self, run):
        with self.admit_pump() as pump:
            return pump(run)

    def _pump(self, run, reserved=None, automatic=False):
        event = reserved if reserved is not None else self.store.reserve_event(run)
        if event is None:
            return {"delivered":False,"empty":True}
        # Count before transport construction/connect; failures remain observable.
        self.attempts += 1
        payload = bytes(event["payload"])
        timestamp = str(int(self.clock()))
        digest = hmac.new(self._secret.encode(),timestamp.encode()+b"."+payload,hashlib.sha256).hexdigest()
        callback = self.callback
        connection = http.client.HTTPConnection(callback.host,callback.port,timeout=1)
        delivered = False
        deadline = None
        expired = threading.Event()
        try:
            address = ipaddress.ip_address(callback.host)
            connection.sock = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET,socket.SOCK_STREAM)
            connection.sock.settimeout(1)
            transport = connection.sock
            def interrupt():
                expired.set()
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            # Socket timeouts alone renew for each byte of a slow reply.
            deadline = threading.Timer(2,interrupt)
            deadline.start()
            # Numeric socket address, no resolver/proxy or redirect facility.
            connection.sock.connect((callback.host,callback.port))
            connection.request("POST",callback.path,body=payload,headers={
                "Content-Type":"application/json","Connection":"close",
                "Stripe-Signature":f"t={timestamp},v1={digest}",
                "X-Local-Simulator-Instance":self.store.instance_id,
                "X-Local-Simulator-Run":run,
            })
            response = connection.getresponse()
            body = response.read(MAX_BODY+1)
            delivered = (200 <= response.status < 300 and len(body) <= MAX_BODY
                         and response.length in (None,0))
        except (OSError,http.client.HTTPException):
            delivered = False
        finally:
            if deadline is not None:
                deadline.cancel()
                deadline.join()
            connection.close()
        delivered = delivered and not expired.is_set()
        scheduled = automatic or event["delivery_state"].startswith("retry:")
        retry_at = (int(self.clock()) + min(3600,self.retry_delay * (2 ** (event["attempts"]-1)))
                    if scheduled and not delivered and event["attempts"] < self.max_attempts else None)
        self.store.settle_event(event["delivery_id"],delivered,retry_at)
        return {"delivered":delivered,"empty":False,"event_id":event["event_id"],"attempts":event["attempts"]}
