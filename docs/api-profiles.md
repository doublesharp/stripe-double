# Stripe API profiles

Verified against official Stripe references on 29 October 2026. The default is
`2026-10-28.endive`, the latest stable API version. Preview releases and arbitrary
version strings are unsupported. This implements the simulator's existing finite
provider subset under that version, not the complete Stripe API.

## Current Endive profile

- API version: `2026-10-28.endive`.
- Protocol profile: `stripe_standard_us_usd_endive_v1`.
- Bootstrap schema: 2; includes `api_version` in the private bootstrap.
- Account resources remain the supported Standard US/USD Accounts v1 subset.
- PaymentIntent creation uses `allowed_payment_method_types[]=card`. Omitting
  it selects the simulator's fixed card-only payment-method configuration.
- `payment_method_types` and `payment_method_types[]` are rejected with HTTP 400,
  code `payment_method_types_no_longer_supported`. The response's computed
  `payment_method_types: ["card"]` property remains supported.
- Explicit `us_bank_account` supports synthetic verification, mandate states,
  asynchronous processing and controlled success/failure. No real bank
  details, verification or customer consent are collected. Other methods fail explicitly.
- Full manual capture, cancellation, explicit refunds, application-fee refunds,
  Account Sessions, capabilities and controlled signed webhooks remain supported.
  Cancellation creates no synthetic refund, consistent with the Basil change.

The reviewed Endive change removes a writable PaymentIntent parameter; replacing
only the version label would incorrectly accept requests Stripe now rejects.
The existing HTTP tests run against Endive with the new form. They retain the
capture, refund, idempotency, restart, capability and webhook checks.

## Acacia compatibility

`--api-version 2025-02-24.acacia` explicitly selects the original
`stripe_standard_us_usd_test_v1` profile. Its strict bootstrap schema remains 1,
with the original fields and no `api_version` field. PaymentIntent creation
continues to require `payment_method_types[]=card`. Webhooks retain Acacia in
`api_version`.

This compatibility path does not upgrade any consumer's real Stripe API version.
It preserves the original modeled behavior and its existing limitations.

## Earlier Endive profile

`--api-version 2026-09-30.endive` selects the earlier supported Endive profile.
It uses the same finite Endive behavior and isolates persisted state and webhook
versions per instance. This is not full Stripe SDK or checkout coverage.

## Durable profile binding

New databases use state schema 2 and store one API version in the instance row.
All scenario runs, request validation and webhook events use that instance's
profile. Restart without a requested version reuses it. An explicit conflicting
version refuses startup; starting another profile requires a fresh state file.
The idempotency fingerprint continues to include the request API version.

Original schema-1 databases reopen as Acacia without a schema migration or any
relabeling of stored objects and original webhook bytes. Unknown schema versions,
unknown stored API versions, and malformed instance metadata fail closed. A
missing HTTP Stripe-Version header never selects the default implicitly.

## Scope reviewed

The modeled subset also includes synthetic Accounts v2 merchant identity and
requirement controls, payouts, and a fixed-rate tax fixture. It does not model
actual identity verification, Balance Settings, real tax calculation, browser
SDK behavior, full subscription billing, or payment methods beyond card and
the limited US bank-account flow. A synthetic subscription fixture supplies
finite reads and lifecycle events for integration tests. New fields or products
in those APIs do not become supported merely because the default version advances.

The simulator retains deliberately limited projections. Source/fixture and local
transport tests establish this modeled behavior, not genuine Stripe equivalence.

## Official references

- [Current Stripe version and versioning](https://docs.stripe.com/api/versioning).
- [Basil changelog](https://docs.stripe.com/changelog/basil).
- [Clover changelog](https://docs.stripe.com/changelog/clover).
- [Dahlia changelog](https://docs.stripe.com/changelog/dahlia).
- [Endive changelog](https://docs.stripe.com/changelog/endive).
- [Endive PaymentIntent parameter removal](https://docs.stripe.com/changelog/endive/2026-09-30/removes-the-payment-method-types-parameter-from-payment-intents-and-setup-intents).
- [Current PaymentIntent creation](https://docs.stripe.com/api/payment_intents/create?api-version=2026-10-28.endive).
- [Basil cancellation/refund change](https://docs.stripe.com/changelog/basil/2025-03-31/remove-refund-from-partial-capture-and-payment-cancellation-flow).
