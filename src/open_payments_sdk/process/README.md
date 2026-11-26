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

**NOTE**: there is nothing stopping you simply executing the grant continuation and outgoing payments on receipt of the merchant response, but you should also execute the following:

1. Validate the `hash` received from the merchant which is derived from the nonce / key, a finish ID, interactive reference, and authentication server URL, all of which are stored in the outgoing payment grant.
2. After payment, validate that the amount - with any transaction fees factored in - has been transferred to the recipient.

Only then should you consider the transaction successfully completed.

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

