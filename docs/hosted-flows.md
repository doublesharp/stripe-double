# Local hosted flows

StripeDouble provides synthetic loopback-hosted Checkout, customer portal and
Connect OAuth flows. They are test interfaces and never contact Stripe or move
money. Their provider resources, API versions and webhook events remain scoped
to the originating instance, run and account.

Hosted URLs use volatile, purpose-bound capabilities. They are not written to
provider state; they expire and are revoked on restart, run reset and shutdown.
An authenticated API read can issue a fresh URL for an open session. Browser
forms require a same-origin POST and a capability-bound CSRF token; mutations
also check the current provider version. Pages have no third-party resources,
do not accept card or bank data, and suppress caching and referrer leakage.
Redirects are allowed only to absolute HTTP(S) URLs with a literal loopback
address and explicit port. Existing API requests may still contain other
return URLs, but a local hosted action refuses to redirect to them.

Hosted subscription Checkout presents a synthetic completion action and a
cancel return. Completion uses the canonical Checkout transaction and emits its
existing events. Cancel returns to the caller without completing the session.
The customer portal shows only the selected customer's account-scoped
subscriptions and invoices. Its supported cancellation control schedules the
subscription to cancel at period end; the next explicit renewal action applies
that cancellation. An explicit portal configuration must permit cancellation.

The OAuth authorization page lists synthetic Accounts v1 in one run and issues
a short-lived one-use code after approval. The token exchange requires the
instance's synthetic API key and returns a run- and account-scoped volatile
access token. Denial returns the original state without issuing a code. Client
applications remain responsible for their own OAuth state verification and
identity/permission decisions. Redirect destinations and codes are never sent
to Stripe. Browser and API integrations require an adapter that points their
OAuth endpoints to this loopback process and supplies the local run headers.
This does not emulate Stripe.js or Elements.

Build the authorization URL as `http://<listener>/oauth/authorize` with
`client_id=ca_LocalSim<instance UUID without hyphens>`, `response_type=code`,
`scope=read_write`, the application's `state` and `redirect_uri`, and the
simulation `run` UUID. Exchange the returned code by POSTing form fields
`grant_type=authorization_code` and `code` to `/oauth/token`, with the normal
instance, run, version and bootstrap API-key headers. The SDK's default
Connect hostname must be redirected by the consuming application's local
adapter; never send simulator credentials or codes to Stripe.

The synthetic access token supports scoped customer, subscription, invoice,
payment intent, charge, refund and balance routes, plus retrieval of its own
connected account. Other routes fail closed. Codes are single-use, and access
tokens expire after one hour or when their run or server is retired.

Open Checkout sessions created before hosted browser support have no stored
success or cancel destination. Create a new session to use the hosted page for
those sessions. A simulation does not qualify real Stripe OAuth, hosted Checkout
or customer portal behavior; these require separate live-provider qualification.
