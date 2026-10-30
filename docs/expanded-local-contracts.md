# Expanded local contracts

This increment covers merchant and platform billing initiation, automatic
local webhooks, and additional Stripe resource simulations. These
remain synthetic API contracts, not claims of real provider equivalence.

## Billing

Portal configurations are account scoped and durable. The supported feature
projection includes a dedicated period-end cancellation policy. Checkout
supports one existing monthly or annual USD price and an
optional connected-account application-fee percentage. Completion creates
linked synthetic resources atomically. Synthetic `LOCAL_` promotion fixtures
support one percentage discount with `once` or `forever` duration; pass their
ID as `discounts[0][promotion_code]`. Checkout, subscription and invoice reads
can expand their associated `discounts`. Once a period is due, a versioned
subscription control creates a renewal invoice with either a paid outcome or
an open failed outcome. Failed invoices require an explicit retry before the
subscription may recover or cancel. Monthly and annual periods retain their
original calendar-day anchor. No automatic billing clock, proration, trial,
multiple-item subscription, or real provider billing is modeled.

A run-scoped admin control configures a
synthetic account tax rate in basis points; without it, automatic tax fails
closed. The configured rate is captured when Checkout is created and applied
to its one price as a fixed exclusive rate. This is a test calculation, not a
jurisdiction, registration, tax determination or collected tax identity.
Address and tax-ID request flags are recorded but no actual details are collected
or verified. Unsupported Checkout options fail closed. Production database
and live-provider qualification are separate.

## Automatic webhooks

Manual delivery stays the default. When enabled with a pinned literal-loopback
callback, the worker starts idle. After the consumer installs the current boot's
webhook secret, `POST /__simulator/v1/runs/{run}/webhooks/start` with an empty
JSON body activates the worker for all active runs. Activation is required again
after restart because credentials rotate. The worker claims each newly queued
event once by default. Optional `--webhook-max-attempts` (1–10) and
`--webhook-retry-delay` (1–3600 seconds) enable exponential backoff, capped at
one hour. The due time and attempt count survive restart. The worker retries
failed deliveries but never automatically replays an interrupted in-flight
attempt; those require inspection and manual action. A manual retry of a
scheduled event retains its automatic policy. The worker stops and joins before
the store closes. Reset and manual delivery share its reservation lock,
preserving run isolation.

## Payout model

The expanded synthetic balance contract credits a completed charge to the
charge account's pending balance, net of the configured synthetic processing
fee and any application fee. The application fee is credited to the platform's
pending balance. A version-checked admin action settles transactions whose
configured delay has elapsed (default 48 hours); pending funds cannot be paid
out. Card and bank-account sources remain separate for payout purposes.
Successful
refunds debit the merchant's available balance immediately, even if that makes
it negative; pending, failed and canceled refunds do not debit it. A fee refund
debits the platform and credits the merchant. A dispute reserves the disputed
amount from available funds and a win releases it. Those debits are synthetic
test rules and do not assert real settlement, tax or fee calculation.

Each new balance transaction is scoped to its run and provider account. Older
charges already stored before this feature are not retroactively credited.
Idempotent replays do not create another transaction. All resource, outbox and
balance changes for a mutation must commit or roll back together.

An administrator funds a synthetic account balance using a version-checked
control. Available funds are separate per provider account and run. Creating a
pending USD payout reserves funds atomically; a version-checked failure or a
cancellation returns the reservation, while a paid outcome consumes it.
Payout events and state survive restart; repeated idempotency keys replay the
original response, and stale lifecycle controls fail. No actual bank details,
real money movement, real settlement schedules, provider processing costs, or
application ledger are modeled. Processing fees are configurable synthetic
fixtures, with zero processing fees by default.

## Bank payments and Accounts v2

Endive accepts explicit `us_bank_account` PaymentIntents with automatic capture
and the synthetic `pm_usBankAccount_processing` and
`pm_usBankAccount_unverified` identifiers. The latter exercises a synthetic
three-attempt descriptor-code verification flow using `SM11AA`. Synthetic
mandates transition from pending to active and become inactive after an
unauthorized debit or dispute. Account-scoped controls resolve payment and
dispute outcomes. No real bank details, customer consent, verification, or
settlement are collected or established. Acacia remains card-only.

Accounts v2 accepts a JSON request for the synthetic US merchant/card capability
subset, using only `@example.test` contact addresses and synthetic display
names. `include` exposes the requested identity, merchant configuration and
requirements projections. A version-checked admin control toggles synthetic
identity requirements and capability status. Account creation and requirement
changes produce thin v2 events with a retrievable account reference. No real
identity fields or verification are accepted, and these accounts do not
automatically become v1 connected accounts. Other Stripe products and fields
fail closed.

Stripe references for the modeled shapes: [portal configuration creation](https://docs.stripe.com/api/customer_portal/configurations/create),
[payout creation](https://docs.stripe.com/api/payouts/create),
[ACH payment lifecycle](https://docs.stripe.com/payments/ach-direct-debit/accept-a-payment?payment-ui=direct-api),
and [Accounts v2](https://docs.stripe.com/api/v2/core/accounts/create).
Local behavior is intentionally narrower than those provider APIs.
