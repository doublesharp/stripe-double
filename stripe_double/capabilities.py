"""Boot-local opaque secrets and purpose-bound component leases."""
from dataclasses import dataclass
import secrets
import threading
from .wire import need, PURPOSES


@dataclass(frozen=True, repr=False)
class Lease:
    run: str
    account: str
    purpose: str
    resource: str
    expires: float


class CapabilityStore:
    def __init__(self, clock):
        self.clock = clock
        self._secrets = {}
        self._controls = {}
        self._lock = threading.RLock()

    def __repr__(self):
        return "CapabilityStore([REDACTED])"

    def _purge(self):
        now = self.clock()
        for items in (self._secrets, self._controls):
            for key in list(items):
                if items[key].expires <= now:
                    del items[key]

    def issue(self, run, account, purpose, resource, prefix=None):
        need(purpose in PURPOSES)
        with self._lock:
            self._purge()
            leases = [*self._secrets.values(), *self._controls.values()]
            need(len(leases) < 1024 and sum(
                (x.run,x.account,x.purpose,x.resource) == (run,account,purpose,resource)
                for x in leases) < 32, 429, "simulator_limit")
            lease = Lease(run, account, purpose, resource, self.clock() + 300)
            token = (prefix or "acs_LocalSim_secret_") + secrets.token_urlsafe(32)
            self._secrets[token] = lease
            return token, lease

    def _checked(self, items, token, run, account, purpose):
        need(isinstance(token, str) and 0 < len(token) <= 512, 403, "simulator_capability")
        lease = items.get(token)
        need(lease is not None and lease.expires > self.clock()
             and (lease.run,lease.account,lease.purpose) == (run,account,purpose),
             403, "simulator_capability")
        return lease

    def exchange(self, token, run, account, purpose):
        with self._lock:
            self._purge()
            lease = self._checked(self._secrets, token, run, account, purpose)
            del self._secrets[token]
            control = "sim_ctl_" + secrets.token_urlsafe(32)
            self._controls[control] = lease
            return control, lease

    def initialize(self, token, run, purpose):
        """Connect embedded components send no account; resolve it only from the secret's own lease."""
        need(purpose in PURPOSES and purpose != "card_payment")
        with self._lock:
            self._purge()
            need(isinstance(token, str) and 0 < len(token) <= 512, 403, "simulator_capability")
            lease = self._secrets.get(token)
            need(lease is not None, 403, "simulator_capability")
            # exchange rechecks run, purpose and expiry under the same lock and
            # consumes the very same single-use secret as the account-explicit route.
            return self.exchange(token, run, lease.account, purpose)

    def consume(self, token, run, account, purpose, retire=False):
        with self._lock:
            self._purge()
            lease = self._checked(self._controls, token, run, account, purpose)
            if retire:
                del self._controls[token]
            return lease

    def expire_run(self, run):
        with self._lock:
            for items in (self._secrets, self._controls):
                for token in list(items):
                    if items[token].run == run:
                        del items[token]

    def close(self):
        with self._lock:
            self._secrets.clear()
            self._controls.clear()
