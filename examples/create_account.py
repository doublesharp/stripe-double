#!/usr/bin/env python3.15
"""Launch an external StripeDouble app and exercise a synthetic account over HTTP."""
import argparse
import http.client
import json
import os
from pathlib import Path
import select
import subprocess
import sys
from urllib.parse import urlencode, urlsplit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True,
                        help="StripeDouble source directory or built .pyz")
    parser.add_argument("--state", type=Path, required=True,
                        help="new state file in an existing owned directory")
    args = parser.parse_args(argv)
    read_fd, write_fd = os.pipe()
    process = None
    connection = None
    try:
        process = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(args.app.resolve()),
            "--state", str(args.state.absolute()), "--create", "--scenario", "STRIPE-LOCAL-01",
            "--bootstrap-fd", str(write_fd)], pass_fds=(write_fd,), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(write_fd)
        write_fd = None
        if not select.select([read_fd], [], [], 5)[0]:
            raise RuntimeError("StripeDouble bootstrap timed out")
        raw = os.read(read_fd, 4096)
        if not raw.endswith(b"\n"):
            raise RuntimeError("StripeDouble bootstrap unavailable")
        bootstrap = json.loads(raw)
        origin = urlsplit(bootstrap["origin"])
        connection = http.client.HTTPConnection(origin.hostname, origin.port, timeout=3)
        form = {
            "country":"US", "default_currency":"usd",
            "capabilities[card_payments][requested]":"true",
            "capabilities[transfers][requested]":"true",
            "controller[losses][payments]":"stripe", "controller[fees][payer]":"account",
            "controller[requirement_collection]":"stripe", "controller[stripe_dashboard][type]":"full",
        }
        connection.request("POST", "/v1/accounts", urlencode(form), {
            "Authorization":"Bearer " + bootstrap["api_key"],
            "Stripe-Version":bootstrap["api_version"],
            "X-Local-Simulator-Instance":bootstrap["instance_id"],
            "X-Local-Simulator-Run":bootstrap["simulation_run_id"],
            "Content-Type":"application/x-www-form-urlencoded",
            "Idempotency-Key":"example-account",
        })
        response = connection.getresponse()
        body = response.read(65537)
        if response.status != 200 or len(body) > 65536:
            raise RuntimeError("Synthetic account creation failed")
        account = json.loads(body)
        if account.get("object") != "account" or not account.get("id", "").startswith("acct_LocalSim"):
            raise RuntimeError("Unexpected synthetic account response")
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        if connection is not None:
            connection.close()
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise RuntimeError("StripeDouble did not stop cleanly") from None
    if process.returncode != 0:
        raise RuntimeError("StripeDouble exited unsuccessfully")
    print("Synthetic account created successfully. No money moved.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, http.client.HTTPException):
        print("StripeDouble example failed; inspect app and owned state configuration.", file=sys.stderr)
        raise SystemExit(1)
