"""Owned-process client for local StripeDouble tests.

Synthetic credentials stay in memory; operations are never retried automatically.
"""
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlencode, urlsplit

from .wire import API_PROFILES, LEGACY_API_VERSION, MAX_BODY, SCENARIOS, SimulatorError, json_object


class TransportUnknown(ConnectionError):
    """A request may have been dispatched; inspect its original outcome before retrying."""


class StripeDouble:
    """Launch a private local simulator. Caller owns its state file and cleanup."""
    def __init__(self, app, state, *, scenario='STRIPE-LOCAL-01', callback=None,
                 create=True, instance=None, run=None, api_version=None, python=None, timeout=5,
                 automatic_webhooks=False, webhook_max_attempts=1, webhook_retry_delay=1):
        self._app = Path(app).resolve(strict=True)
        if not (self._app.is_file() or (self._app / '__main__.py').is_file()):
            raise ValueError('invalid simulator app')
        self._state = Path(state)
        if not self._state.is_absolute():
            raise ValueError('state path must be absolute')
        if type(create) is not bool or (create and (instance is not None or run is not None)) or (
                not create and (not instance or not run)):
            raise ValueError('invalid create or restart selection')
        if api_version is not None and (not isinstance(api_version,str) or api_version not in API_PROFILES):
            raise ValueError('unsupported API profile')
        if type(automatic_webhooks) is not bool or (automatic_webhooks and callback is None):
            raise ValueError('automatic webhooks require a callback')
        if (type(webhook_max_attempts) is not int or not 1 <= webhook_max_attempts <= 10 or
                type(webhook_retry_delay) is not int or not 1 <= webhook_retry_delay <= 3600 or
                (not automatic_webhooks and webhook_max_attempts != 1)):
            raise ValueError('invalid webhook retry policy')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError('timeout must be positive and at most 30 seconds')
        self._python = Path(python or sys.executable).resolve(strict=True)
        self._create, self._scenario = create, scenario
        self._instance, self._run = instance, run
        self._api_version, self._callback, self._timeout = api_version, callback, float(timeout)
        self._automatic_webhooks = automatic_webhooks
        self._webhook_max_attempts, self._webhook_retry_delay = webhook_max_attempts, webhook_retry_delay
        self._process = None
        self._bootstrap = None
        self._used = False
        self.closed = True

    def __repr__(self):
        return 'StripeDouble([REDACTED])'

    def __enter__(self):
        if self._used:
            raise RuntimeError('simulator owner cannot be reused')
        self._used = True
        read_fd, write_fd = os.pipe()
        try:
            args = [str(self._python), '-I', '-S', '-B', str(self._app), '--state', str(self._state),
                    '--bootstrap-fd', str(write_fd)]
            if self._create:
                args.extend(['--create', '--scenario', self._scenario])
            else:
                args.extend(['--instance', self._instance, '--run', self._run])
            if self._api_version is not None:
                args.extend(['--api-version', self._api_version])
            if self._callback is not None:
                args.extend(['--callback', self._callback])
            if self._automatic_webhooks:
                args.extend(['--automatic-webhooks','--webhook-max-attempts',str(self._webhook_max_attempts),
                             '--webhook-retry-delay',str(self._webhook_retry_delay)])
            self._process = subprocess.Popen(args, pass_fds=(write_fd,), stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.close(write_fd)
            write_fd = None
            deadline = time.monotonic() + self._timeout
            raw = bytearray()
            while not raw.endswith(b'\n'):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([read_fd], [], [], remaining)[0]:
                    raise RuntimeError('simulator bootstrap timed out')
                chunk = os.read(read_fd, 4097 - len(raw))
                if not chunk or len(raw) + len(chunk) > 4096:
                    raise RuntimeError('simulator bootstrap unavailable')
                raw.extend(chunk)
            bootstrap = self._validate_bootstrap(bytes(raw[:-1]))
            if (self._create and bootstrap['scenario_id'] != self._scenario) or (
                    not self._create and (bootstrap['instance_id'] != self._instance or
                                          bootstrap['simulation_run_id'] != self._run)) or (
                    self._api_version is not None and bootstrap['api_version'] != self._api_version):
                raise RuntimeError('simulator bootstrap does not match the requested instance')
            self._bootstrap = bootstrap
            self.closed = False
            return self
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception as error:
            if exc is None:
                raise
            exc.add_note('StripeDouble process cleanup failed: ' + type(error).__name__)
        return False

    @staticmethod
    def _validate_bootstrap(raw):
        try:
            value = json_object(raw)
            common = {'schema_version','instance_id','simulation_run_id','scenario_id','platform_account',
                      'protocol_profile','origin','api_key','public_key','admin_token','webhook_secret'}
            if type(value.get('schema_version')) is not int or value.get('scenario_id') not in SCENARIOS:
                raise ValueError('invalid bootstrap')
            if value.get('schema_version') == 1:
                if set(value) != common or value.get('protocol_profile') != API_PROFILES[LEGACY_API_VERSION]:
                    raise ValueError('invalid bootstrap')
                api_version = LEGACY_API_VERSION
            elif value.get('schema_version') == 2:
                if set(value) != common | {'api_version'}:
                    raise ValueError('invalid bootstrap')
                api_version = value['api_version']
                if not isinstance(api_version,str) or API_PROFILES.get(api_version) != value['protocol_profile']:
                    raise ValueError('invalid bootstrap')
            else:
                raise ValueError('invalid bootstrap')
            for field in ('instance_id','simulation_run_id'):
                if str(uuid.UUID(value[field])) != value[field]:
                    raise ValueError('invalid bootstrap identity')
            if value['platform_account'] != 'acct_LocalSim' + value['instance_id'].replace('-', ''):
                raise ValueError('invalid bootstrap identity')
            for field, pattern in [('api_key',r'sk_test_LocalSim[0-9a-f]{32}'),
                                   ('public_key',r'pk_test_LocalSim[0-9a-f]{32}'),
                                   ('admin_token',r'sim_admin_[A-Za-z0-9_-]{43}'),
                                   ('webhook_secret',r'whsec_LocalSim[0-9a-f]{64}')]:
                if not isinstance(value[field],str) or re.fullmatch(pattern,value[field]) is None:
                    raise ValueError('invalid synthetic credentials')
            origin = urlsplit(value['origin'])
            address = ipaddress.ip_address(origin.hostname or '')
            canonical_host = f'[{address}]' if address.version == 6 else str(address)
            if (origin.scheme != 'http' or not address.is_loopback or origin.port is None or origin.port == 0
                    or origin.netloc != f'{canonical_host}:{origin.port}' or origin.path or origin.query or origin.fragment):
                raise ValueError('invalid local origin')
            value['api_version'] = api_version
            return value
        except (SimulatorError, KeyError, TypeError, ValueError, AttributeError) as error:
            raise RuntimeError('invalid simulator bootstrap') from error

    def _active(self):
        if self.closed or self._bootstrap is None or self._process is None:
            raise RuntimeError('simulator owner is closed')
        return self._bootstrap

    @property
    def origin(self):
        return self._active()['origin']

    @property
    def instance_id(self):
        return self._active()['instance_id']

    @property
    def run_id(self):
        return self._active()['simulation_run_id']

    @property
    def api_version(self):
        return self._active()['api_version']

    @property
    def api_key(self):
        return self._active()['api_key']

    @property
    def public_key(self):
        return self._active()['public_key']

    @property
    def webhook_secret(self):
        return self._active()['webhook_secret']

    def request(self, method, path, *, form=None, json_body=None, account=None, key=None, admin=False):
        boot = self._active()
        if method not in {'GET','POST'} or not isinstance(path,str) or not path.startswith('/') or path.startswith('//'):
            raise ValueError('invalid local request')
        target = urlsplit(path)
        if target.scheme or target.netloc or target.fragment or (form is not None and json_body is not None):
            raise ValueError('invalid local request')
        headers = {'Authorization':'Bearer ' + boot['admin_token' if admin else 'api_key'],
                   'Stripe-Version':boot['api_version'], 'X-Local-Simulator-Instance':boot['instance_id'],
                   'X-Local-Simulator-Run':boot['simulation_run_id']}
        if account is not None:
            headers['Stripe-Account'] = account
        if key is not None:
            headers['Idempotency-Key'] = key
        if json_body is not None:
            raw = json.dumps(json_body,separators=(',',':'),allow_nan=False).encode()
            headers['Content-Type'] = 'application/json'
        elif form is not None:
            raw = urlencode(form).encode('ascii')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        else:
            raw = None
        if raw is not None and len(raw) > MAX_BODY:
            raise ValueError('request too large')
        origin = urlsplit(boot['origin'])
        address = ipaddress.ip_address(origin.hostname)
        conn = http.client.HTTPConnection(str(address), origin.port, timeout=self._timeout)
        transport = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
        transport.settimeout(self._timeout)
        conn.sock = transport
        response = None
        def interrupt():
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        deadline = threading.Timer(self._timeout, interrupt)
        deadline.start()
        try:
            transport.connect((str(address),origin.port))
            conn.request(method,path,body=raw,headers=headers)
            response = conn.getresponse()
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise http.client.HTTPException('response too large')
            for name, expected in [('X-Local-Simulator-Instance',boot['instance_id']),
                                   ('X-Local-Simulator-Run',boot['simulation_run_id'])]:
                values = [v for k,v in response.getheaders() if k.lower() == name.lower()]
                required = name.endswith('Instance') or response.status == 200
                if (required and values != [expected]) or (values and values != [expected]):
                    raise http.client.HTTPException('response identity mismatch')
            return response.status, json_object(data)
        except (OSError,http.client.HTTPException,SimulatorError) as error:
            raise TransportUnknown('local simulator response unavailable; do not automatically replay') from error
        finally:
            try:
                deadline.cancel()
                deadline.join()
            finally:
                if response is not None:
                    response.close()
                conn.close()
                transport.close()

    def inspect_run(self):
        return self.request('POST',f'/__simulator/v1/runs/{self.run_id}/inspect',json_body={},admin=True)

    def action(self, action, expected_state_version):
        return self.request('POST',f'/__simulator/v1/runs/{self.run_id}/actions',
                            json_body={'action':action,'expected_state_version':expected_state_version},admin=True)

    def list_events(self, after_ordinal=0):
        return self.request('POST',f'/__simulator/v1/runs/{self.run_id}/events/list',
                            json_body={'after_ordinal':after_ordinal},admin=True)

    def start_automatic_webhooks(self):
        """Activate only after the consumer installs this boot's webhook secret."""
        return self.request('POST',f'/__simulator/v1/runs/{self.run_id}/webhooks/start',json_body={},admin=True)

    def deliver_event(self, delivery_id, expected_state_version):
        return self.request('POST',f'/__simulator/v1/runs/{self.run_id}/events/deliver',
                            json_body={'delivery_id':delivery_id,'expected_state_version':expected_state_version},admin=True)

    def close(self):
        self.closed = True
        self._bootstrap = None
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            code = process.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self._timeout)
            raise RuntimeError('simulator process did not stop within its deadline') from None
        if code != 0:
            raise RuntimeError('simulator process exited unsuccessfully')
