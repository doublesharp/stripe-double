#!/usr/bin/env python3.15
"""StripeDouble: local Stripe simulation. No money moves; bootstrap uses a private pipe."""
import argparse
import os
from pathlib import Path
import select
import signal
import sqlite3
import stat
import sys
import threading
from . import __version__
from .http_api import SimulatorServer
from .state import StateStore
from .webhooks import Callback
from .wire import API_PROFILES, LEGACY_API_VERSION, SCENARIOS, SimulatorError, dumps, need


def bootstrap_pipe(fd):
    need(type(fd) is int and fd >= 3)
    facts = os.fstat(fd)
    need(stat.S_ISFIFO(facts.st_mode) or stat.S_ISSOCK(facts.st_mode))
    return fd


def serve_loopback(args, stop):
    """Own the state, listener and all request workers until an explicit stop."""
    bootstrap_pipe(args.bootstrap_fd)
    path = Path(args.state)
    need(path.is_absolute() and path.parent == path.parent.resolve()
         and not path.is_symlink(),503,'simulator_state')
    # Refuse configuration before allocating a durable state file.
    import ipaddress
    address = ipaddress.ip_address(args.host)
    need(address.is_loopback and str(address) == args.host and 0 <= args.port <= 65535)
    if args.callback is not None:
        Callback.parse(args.callback)
    automatic_webhooks = getattr(args,'automatic_webhooks',False)
    need(not automatic_webhooks or args.callback is not None)
    webhook_max_attempts = getattr(args,'webhook_max_attempts',1)
    webhook_retry_delay = getattr(args,'webhook_retry_delay',1)
    need(type(webhook_max_attempts) is int and 1 <= webhook_max_attempts <= 10 and
         type(webhook_retry_delay) is int and 1 <= webhook_retry_delay <= 3600)
    need(automatic_webhooks or webhook_max_attempts == 1)
    need((args.create and args.scenario in SCENARIOS and args.run is None and args.instance is None)
         or (not args.create and args.scenario is None and args.run is not None and args.instance is not None))
    store = StateStore(path,create=args.create,api_version=args.api_version)
    server = None
    try:
        if args.create:
            run = store.create_run(args.scenario)['simulation_run_id']
        else:
            need(args.instance == store.instance_id,403,'simulator_instance')
            run = store.run(args.run)['simulation_run_id']
        server = SimulatorServer(store,callback=args.callback,host=args.host,port=args.port,
                                 automatic_webhooks=automatic_webhooks,webhook_max_attempts=webhook_max_attempts,
                                 webhook_retry_delay=webhook_retry_delay)
        server.start()
        host,port = server.address
        origin = f'http://[{host}]:{port}' if ':' in host else f'http://{host}:{port}'
        bootstrap = {'schema_version':1 if store.api_version == LEGACY_API_VERSION else 2,'instance_id':store.instance_id,'simulation_run_id':run,
            'scenario_id':store.run(run)['scenario_id'],'platform_account':store.platform_account,
            'protocol_profile':store.protocol_profile,'origin':origin,
            'api_key':server.credentials.api_key,'public_key':server.credentials.public_key,
            'admin_token':server.credentials.admin_token,'webhook_secret':server.credentials.webhook_secret}
        if bootstrap['schema_version'] == 2:
            bootstrap['api_version'] = store.api_version
        data = dumps(bootstrap) + b'\n'
        # Nonblocking write plus a finite wait also bounds an abandoned bootstrap.
        os.set_blocking(args.bootstrap_fd,False)
        need(bool(select.select([], [args.bootstrap_fd], [], 2)[1]),503,'simulator_bootstrap')
        need(os.write(args.bootstrap_fd,data) == len(data),503,'simulator_bootstrap')
        stop.wait()
    finally:
        try:
            if server is not None:
                server.close()
        finally:
            store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="stripe-double", description=__doc__)
    parser.add_argument("--version", action="version", version=f"StripeDouble {__version__}")
    parser.add_argument('--state',required=True,help='absolute path to owned SQLite state; physical parent directory must exist')
    parser.add_argument('--create',action='store_true',help='explicitly create new state and a run')
    parser.add_argument('--scenario',choices=sorted(SCENARIOS))
    parser.add_argument('--api-version',choices=sorted(API_PROFILES),
                        help='new state defaults to current stable Stripe; restart keeps the stored version')
    parser.add_argument('--run',help='existing run UUID on restart')
    parser.add_argument('--instance',help='expected existing instance UUID on restart')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--callback',help='optional startup-pinned literal loopback HTTP callback')
    parser.add_argument('--automatic-webhooks',action='store_true',help='enable automatic webhook delivery; requires --callback')
    parser.add_argument('--webhook-max-attempts',type=int,default=1,help='automatic attempts per event, 1-10 (default 1)')
    parser.add_argument('--webhook-retry-delay',type=int,default=1,help='initial retry delay in seconds, 1-3600 (default 1)')
    parser.add_argument('--bootstrap-fd',type=int,required=True,help='inherited private pipe/socket, never stdout or a file')
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = {sig:signal.signal(sig,lambda *_:stop.set()) for sig in (signal.SIGINT,signal.SIGTERM)}
    try:
        serve_loopback(args,stop)
        return 0
    except (SimulatorError,OSError,ValueError,sqlite3.Error):
        print('StripeDouble unavailable; state and configuration require inspection.',file=sys.stderr)
        return 1
    finally:
        for sig,handler in previous.items():
            signal.signal(sig,handler)


if __name__ == '__main__':
    raise SystemExit(main())
