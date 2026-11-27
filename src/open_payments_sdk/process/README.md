# Structured processes

The functions in this module support commonly-used payments processes. For anything more complex, or where you wish for greater control, you can fall back to the standard API.

## Standard payment processes

1. **One-off**: a single payment from a sender to a recipient.
2. **Recurring**: repeated payments from a sender to a recipient.
3. **Conditional**: a payment whose initiation depends on an application-defined condition.

There are additional characteristics as well, such as **split payments** between multiple recipients, which may be required.

Each of these has a common set of steps which are performed on behalf of the corresponding payments actors (cf. https://openpayments.dev/concepts/op-flow/):

1. Get the sender's and recipient's wallet address information
2. Request an _Incoming Payment Grant_
3. Create an _Incoming Payment_
4. Request a _Quote Grant_
5. Create a _Quote_
6. Request an interactive _Outgoing Payment Grant_
7. Start interaction with the sender
8. Finish interaction with the sender
9. Request a _Grant Continuation_
10. Create an _Outgoing Payment_

**NOTE**: Steps 7 and 8 are discontinuous and stateless. This set of convenience functions does **NOT** store your transaction state. That is your responsibility.

## Development requirements

There are several "externalities" necessary to use these functions:

1. You need to develop some mechanism for storing asynchronous / stateless information (e.g. text files or a database) received during the grant-making process.
2. This helper signs requests using the configured recipient account, so its wallet address, private key and registered key ID must be available.
3. Store payment state and the initial outgoing payment grant response before redirecting the sender. Correlate the returned callback with that stored transaction.
4. Provide a callback endpoint that receives the sender's interaction result and dispatches it for verification and grant continuation.

Before continuing a grant or creating an outgoing payment, validate the callback against its stored interaction context:

1. Validate the received `hash` using the original client nonce, server finish nonce, interaction reference and exact grant initiation endpoint.
2. After payment, validate that the amount - with any transaction fees factored in - has been transferred to the recipient.

Only then should you consider the transaction successfully completed.

### Stored consent and completion attempts

`initiate_payment(amount=...)` creates the incoming payment and quote, requests an outgoing payment grant, and returns its interaction redirect URI. It generates a fresh unpredictable `client_nonce` and records the exact `grant_endpoint` used for initiation. These values, the server's `finish_id`, continuation credentials and payment/quote references are part of `pending_payment`. Persist that model before handing the redirect to the sender. Its serialized data contains credentials and must be kept in your protected transaction store.

The existing `get_purchase_endpoint(amount=...)` name remains a compatibility alias for `initiate_payment`. Both names use the same state and initiation guard. The existing `buyer` and `seller` actor names refer to the sender and recipient respectively; existing constructor arguments and saved-state fields remain supported.

The claimed initiation records `incoming_amount` from the validated request using the recipient's assets. As each stage succeeds, it snapshots the full `incoming_payment`, `quote` and `outgoing_grant` responses, retaining their resource IDs and setting `quoted_amount` to the sender's quoted debit amount. Sending and receiving assets remain independent. Quote expiry strings and grant continuation waits are preserved for application decisions. Persist partial state when initiation raises; a missing response does not establish that the provider created no resource, and the same transaction cannot be restarted implicitly.

This redirect helper requires interaction instructions from the outgoing grant. If the server issues an access token without requiring interaction, the helper raises `ValueError` and retains that approved response and its continuation credentials in `outgoing_grant`, `continue_url` and `continue_id`. The application can use the stored quote and token with the standard API for explicit outgoing creation. The processor does not force another consent request or create a payment automatically while preparing a redirect.

`complete_payment` verifies the callback against that stored context and checks that its sender and recipient match the processor. Missing, malformed or mismatched callbacks and incomplete records are rejected before continuation or outgoing creation. Old records without the original nonce and endpoint cannot be resumed by guessing them from the transaction ID or current wallet metadata.

Each transaction allows one initiation and one completion attempt. The processor sets `completion_started` before sending the continuation and keeps it set on success or failure, including an uncertain HTTP outcome. Persist the mutated state even when completion raises; reconcile uncertain outcomes with the provider before considering another payment attempt.

The incoming-payment and quote helpers validate their resource inputs before requesting a grant. Payment initiation also validates its amount before generating a nonce or claiming the initiation attempt. Integer amounts are converted to decimal strings; booleans, negative or overflowing amounts and invalid quote receivers are rejected by the existing resource models.

Validated grant responses are retained as snapshots in `pending_payment.incoming_grant` and `pending_payment.quote_grant` before resource creation. Each helper requires an issued access token; a pending grant raises `ValueError` without attempting the incoming payment or quote. Its interaction instructions, continuation credentials and wait remain available in the saved grant for explicit application handling through the standard API. Approved grants retain their token management and expiry metadata even if resource creation fails. These snapshots are omitted from ordinary representations and included in explicit protected state dumps. The helpers do not fall back to a token from an earlier response.

After a validated continuation, `pending_payment.continuation_response` retains a snapshot of the full result, including any issued token and its management URI, permissions and expiry, subject data, and continuation wait. `continue_url` and `continue_id` are updated to the replacement credentials before outgoing creation. These updates are in memory; persist the mutated model when the call returns or raises, including when creation fails. A pending or subject-only continuation raises `ValueError` before creating a payment; its response and replacement continuation credentials remain available for the application's explicit handling. Respect the returned wait before any further continuation request.

Successful creation also stores a snapshot in `pending_payment.outgoing_payment` before returning the payment. Its ID and amounts survive JSON serialization and recovery, and changing the returned payment does not change that snapshot. Old records without these two optional fields still load. These snapshots are omitted from ordinary model representations; explicit dumps contain credentials and payment data and belong in protected storage.

Retaining a result does not reset `completion_started` or make a repeated callback safe to replay. After an HTTP or validation failure, an absent outgoing result does not establish that the provider created no payment. Use the stored context for reconciliation. The processor does not poll pending grants, rotate tokens automatically or restart a failed completion.

The in-process guard synchronizes calls sharing the same pending model. Your application must atomically claim the persisted record before completion to coordinate separate workers, reloaded records or copied models, and retain that durable claim across crashes. The SDK does not provide a database transaction or replay store.

## Architecture decisions

This module is opinionated and implements the process in ways that may conflict with your way of working. If you find this challenging, the API is available to you.

One specific example is that of using [ULIDs](https://github.com/ulid/spec) in preference to UUIDs. ULIDs are prefered as they are:

- more memory-efficient than UUIDs, being a 26-character string rather than a 36-character string,
- lexicographically sortable as each string generated has a temporal sequence,
- case-insensitive and is URL safe.

This is used in generating reference IDs.

## Current state of this module

Only one-off payments are currently developed. This note will be updated as progress is made.

## Tests

These modules are used to test the functionality of the API and general utilities in the SDK.

