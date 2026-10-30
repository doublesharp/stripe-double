# Near-term simulator work

This increment covers connected-account card payments and subscription billing,
including recurring prices, customers, Checkout, subscription and invoice reads,
hosted flows, Elements session creation, and signed webhooks.

Product integrations are compatibility checks, not simulator dependencies or domain
models. The fixture accepts optional caller-defined `client_reference_id` and
`metadata`; it assigns no application-specific meaning to either.

## Shared requirements

- A Python test harness owns one explicit local app process and its private
  bootstrap; provides bounded local HTTP, control actions, inspection and
  teardown; never retries an uncertain mutation. All supported API profiles continue to
  bind to their own state and webhook versions.
- Card scenarios exercise decline, authentication required, processing and
  controlled resolution. Resource-version checks prevent stale actions.
- Targeted pending refund success, failure and cancellation are atomic and
  emit appropriately scoped events. Failed/canceled amounts become available
  for a subsequent refund without allowing over-refund.
- Admins can inspect and deliver a specific outbox entry, including deliberate
  reordering, while retaining original event bytes and observable attempts.
- Account readiness is adjustable independently per synthetic account; no
  application authentication, entitlement or financial-ledger data is stored.
- Billing/Checkout support must use a defined request and response subset,
  synthetic identities and local controls, and
  clearly refuse unmodeled tax, browser or settlement behavior. Signed event
  payloads and subsequent canonical reads must identify the same resources.

All HTTP listeners and callbacks remain literal loopback, and all credentials
remain synthetic. Existing Acacia clients, v1 state, version isolation, idempotent
replay, bounded worker/response behavior and no automatic write retries remain
part of the acceptance gate. The harness must not persist credentials or print
bootstrap data. No real Stripe traffic, actual payment data or provider account
is needed for these tests.

Acceptance includes behavioral fail-to-pass regressions, profile tests,
real HTTP/process lifecycle, persisted restart and duplicate/reordered webhook
cases and consumer integration checks.
Browser and genuine-Stripe conformance are separate gates; support claims must
name the exact implemented subset.

## Implemented and remaining

The harness, card outcomes, targeted refunds, account controls and selected
outbox delivery are implemented. A synthetic subscription seed creates linked
customer, annual price, subscription, invoice, payment, charge and completed
Checkout resources; actions can mark the subscription past due, recover it or
cancel it. Reads are scoped by account and include finite invoice and charge
lists.

Hosted and Elements subscription Checkout session creation accepts a single
existing synthetic monthly or annual USD price, an optional synthetic customer, and the
limited forms used by the checked consumers. Customer creation accepts only
`@example.test` addresses. An admin can inspect an open session and use
`checkout/sessions/{id}/actions` with `action: complete` or `expire` and its
`expected_provider_version`. Completion atomically creates linked subscription,
invoice, payment and charge resources and queues corresponding signed events;
expiry is explicit, and completion refuses sessions past their expiry time.
Session listing and synthetic billing portal session creation are supported.

Hosted Checkout and customer portal URLs open local synthetic pages; the portal
can schedule or resume cancellation. See [hosted flows](hosted-flows.md) for
the browser and OAuth contracts. An Elements `client_secret` is a volatile
synthetic placeholder, not a Stripe.js credential; it is not stored and a replay
may return a new placeholder. Expiry is still an explicit lifecycle operation.
Explicit synthetic percentage promotion fixtures are supported; see
[expanded local contracts](expanded-local-contracts.md). The bounded portal configuration and application-fee percentage
contracts support billing initiation. Tax requires an explicitly
configured synthetic fixed rate and never establishes real tax treatment or
collects a billing address or tax ID. Genuine Stripe
behavior, application databases, production settlement and complete application
journeys remain separate verification gates.
