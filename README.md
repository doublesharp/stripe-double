# StripeDouble

**A local, stateful Stripe API simulator for development and integration tests.**

[![Python 3.15](https://img.shields.io/badge/Python-3.15-blue?logo=python&logoColor=white)](https://www.python.org/)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)
![Dependencies](https://img.shields.io/badge/runtime%20dependencies-none-brightgreen)
![Mode](https://img.shields.io/badge/payments-simulated-purple)
![License](https://img.shields.io/badge/license-all%20rights%20reserved-lightgrey)

StripeDouble lets you exercise selected payment and billing flows on your own
machine with synthetic accounts, resources, and events. It is useful when you
want repeatable tests for subscription Checkout, connected accounts, refunds,
payouts, webhooks, and failure handling without moving money or contacting Stripe.

**StripeDouble is an independent project. It is not affiliated with, endorsed
by, or associated with Stripe.** Stripe is a trademark of Stripe, Inc.

## What it can do

| Area | Supported local behavior |
| --- | --- |
| Checkout and billing | Subscription Checkout, linked invoices and payments, explicit renewals, failure recovery, promotions, and a customer portal for period-end cancellation. |
| Connect | Synthetic connected accounts, readiness controls, hosted OAuth authorization, and scoped access tokens. |
| Payments | Card PaymentIntents, confirmation and authentication outcomes, captures, cancellations, charges, refunds, and limited US bank-payment scenarios. |
| Payouts and balances | Synthetic balances, settlement controls, payout creation, cancellation, failure, and completion. |
| Webhooks | Signed events in a durable outbox, manual delivery, optional automatic delivery with configurable retries, and replay and ordering controls. |
| Isolation | Separate runs, account-scoped resources, request idempotency, version checks, private bootstrap credentials, and loopback-only networking. |

StripeDouble models a **bounded subset** of Stripe behavior. It does not collect
real card or bank details, verify identities, calculate real tax, move money,
or emulate Stripe.js and Elements. Its results do not establish live Stripe
compatibility; qualify production integrations separately.

## Requirements

- macOS or Linux and Python **3.15** with SQLite support; development is pinned
  to Python 3.15.2.
- No third-party Python runtime or build packages, Stripe account, or real keys.
- A private, writable directory for simulator state. The simulator uses Unix
  file ownership checks, file locking, and inherited file descriptors.

## Quick start

From the repository root, run the included end-to-end example:

```sh
python3.15 -B -m stripe_double --version
mkdir -p "$HOME/stripe-double-state"
python3.15 -B examples/create_account.py \
  --app "$PWD" \
  --state "$HOME/stripe-double-state/example.sqlite3"
```

The example starts StripeDouble, receives its synthetic credentials through a
private pipe, creates a synthetic account over loopback HTTP, and shuts it down.
It prints no credentials. The state file must be new and its parent must be an
existing physical directory; choose a different filename for each fresh run.

For a self-contained executable archive:

```sh
mkdir -p "$HOME/stripe-double-build"
python3.15 -B tools/build_app.py --output "$HOME/stripe-double-build/stripe-double.pyz"
python3.15 -B "$HOME/stripe-double-build/stripe-double.pyz" --help
```

The output file must not already exist. A target machine needs Python 3.15
with SQLite, but does not need a checkout or pip packages.

## Use it in tests

The Python harness starts and owns the simulator process, keeps bootstrap
credentials in memory, and closes the process when the context exits:

```python
from stripe_double.harness import StripeDouble

with StripeDouble('/absolute/path/to/stripe-double',
                  '/absolute/path/to/new-state.sqlite3') as stripe:
    status, run = stripe.inspect_run()
    status, events = stripe.list_events()
```

The first path can be the source directory or a built `.pyz`. The state file's
parent must already exist. The harness exposes `request`, `action`, and
`deliver_event` for provider requests and simulator controls; it never retries
an uncertain mutation automatically. See [the harness tests](tests/test_harness.py)
and [the local API tests](tests/test_near_term.py) for complete examples.

An external launcher can instead start the app with `--state`, `--create`,
`--scenario STRIPE-LOCAL-01`, and an inherited `--bootstrap-fd` for a private
pipe or Unix socket. [The account example](examples/create_account.py) shows
the process lifecycle and HTTP headers. Bootstrap is not written to stdout or
regular files. Restart an existing state file with `--instance`, `--run`, and
`--bootstrap-fd`; omit `--create` and `--scenario`.

## API and browser flows

The HTTP listener defaults to an ephemeral `127.0.0.1` port. API requests use
the synthetic bearer credential, the instance and run headers, and the selected
`Stripe-Version`. Most POST requests also require an idempotency key. Admin
controls use a separate credential. Stripe SDKs need an adapter that supplies
these headers and directs requests to the local listener; changing only the base
URL is insufficient.

The default API profile is **2026-10-28.endive**. The simulator also supports
`2026-09-30.endive` and an explicit `2025-02-24.acacia` compatibility profile.
The selected version is stored with the instance and cannot be changed on
restart. See [API profiles](docs/api-profiles.md).

Hosted Checkout, the customer portal, and Connect OAuth use synthetic loopback
pages. Browser capabilities expire, and redirects must target literal loopback
addresses with explicit ports. See [hosted flows](docs/hosted-flows.md) for
setup, supported actions, and OAuth adapter requirements.

## Webhooks and scenarios

Webhooks are stored in a durable outbox and can be delivered to one configured
literal-loopback callback. Delivery is manual by default. To enable automatic
transport, start the simulator with `--automatic-webhooks` and `--callback`,
install the current bootstrap webhook secret in your receiver, then activate
webhooks for the run. Configure bounded retries with `--webhook-max-attempts`
and `--webhook-retry-delay`; interrupted in-flight attempts require inspection
and manual action. The harness provides `start_automatic_webhooks()`.

Nine `STRIPE-LOCAL-01` through `STRIPE-LOCAL-09` scenario presets and explicit
controls let tests choose outcomes. See [local contracts](docs/expanded-local-contracts.md),
[simulator behavior](docs/near-term-gaps.md), and
[scenario definitions](stripe_double/scenarios.py) for the supported scope.

## Run the test suite

Set `TMPDIR` to an owned disposable directory, then run:

```sh
PYTHONDONTWRITEBYTECODE=1 python3.15 -B -m unittest discover -s tests -p 'test_*.py' -v
```

The suite covers HTTP, SQLite, process lifecycle, webhook delivery, API
profiles, and the standalone archive. Browser tests are optional and require
an installed Playwright module configured with `STRIPE_DOUBLE_PLAYWRIGHT_MODULE`.
See [the browser test](tests/test_hosted_browser.py) for its local setup.

## License

Copyright © 2026 DoubleSharp. All rights reserved. No open-source license has
been selected; see [LICENSE](LICENSE).
