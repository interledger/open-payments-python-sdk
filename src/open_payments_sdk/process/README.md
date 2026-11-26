# Structured processes

The functions in this module support commonly-used payments processes. For anything more complex, or where you wish for greater control, you can fall back to the standard API.

## Standard payment processes

1. **One-off**: a payment - usually of purchase and sale - between a buyer and seller which happens once.
2. **Recurring**: a payment - usually a subscription, or structured fee - between a buyer and seller.
3. **Conditional**: a payment which is conditional on some additional factor - e.g. the seller is required to perform some task, or a threshold of purchases are required - before funds are transfered.

There are additional characteristics as well, such as **split payments** between multiple recipients, which may be required.

Each of these has a common set of steps which are performed on behalf of the corresponding payments actors (cf. https://openpayments.dev/concepts/op-flow/):

1. Get purchaser / buyer's wallet address information
2. Request an _Incoming Payment Grant_
3. Create an _Incoming Payment_
4. Request a _Quote Grant_
5. Create a _Quote_
6. Request an interactive _Outgoing Payment Grant_
7. Start interaction with the purchaser
8. Finish interaction with the purchaser
9. Request a _Grant Continuation_
10. Create an _Outgoing Payment_

**NOTE**: Steps 7 and 8 are discontinuous and stateless. This set of convenience functions does **NOT** store your transaction state. That is your responsibility.

## Development requirements

There are several "externalities" necessary to use these functions:

1. You need to develop some mechanism for storing asynchronous / stateless information (e.g. text files or a database) received during the grant-making process.
2. You need to have the recipient / seller's information in advance, including their private key to permit independent grant requests on their behalf (`wallet_address`, `private_key`, `key_id`).
3. You must store the initial outgoing payment grant response to recover the purchase request. The merchant response will return a reference ID you provided, and which you can use to recover the specified transaction.
4. You need a web-based client which can receive the purchaser's merchant / wallet response and dispatch that response for further processing. The purchaser's merchant response will be directed to a specified URL endpoint.

Before continuing a grant or creating an outgoing payment, validate the callback against its stored interaction context:

1. Validate the received `hash` using the original client nonce, server finish nonce, interaction reference and exact grant initiation endpoint.
2. After payment, validate that the amount - with any transaction fees factored in - has been transferred to the recipient.

Only then should you consider the transaction successfully completed.

### Stored consent and completion attempts

`get_purchase_endpoint` generates a fresh unpredictable `client_nonce` and records the exact `grant_endpoint` used for initiation. These values, the server's `finish_id`, continuation credentials and payment/quote references are part of `pending_payment`. Persist that model before handing the redirect to the buyer. Its serialized data contains credentials and must be kept in your protected transaction store.

`complete_payment` verifies the callback against that stored context and checks that its buyer and seller match the processor. Missing, malformed or mismatched callbacks and incomplete records are rejected before continuation or outgoing creation. Old records without the original nonce and endpoint cannot be resumed by guessing them from the transaction ID or current wallet metadata.

Each transaction allows one initiation and one completion attempt. The processor sets `completion_started` before sending the continuation and keeps it set on success or failure, including an uncertain HTTP outcome. Persist the mutated state even when completion raises; reconcile uncertain outcomes with the provider before considering another payment attempt.

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

