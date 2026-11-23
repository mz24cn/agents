# Model-side failures: replay with backoff, fail fast only on a named reason

Changed rule. A model API failure used to be replayed only if its HTTP status was
on a list of "transient" codes (408, 425, 429, 5xx). Everything else -- a 400 with
a body we could not read a reason from, a 200 stream that ended without a single
token, a provider payload whose shape made our parser raise -- ended the
generation on the first attempt.

That put the burden of proof on the failure being *recognised*, and most model-side
failures are not recognisable: gateways answer 400 with their own HTML, proxies
truncate SSE bodies, servers return `{"error": ...}` shapes nobody documented.
Those are momentary far more often than they are permanent, so the rule is now the
other way round:

> A model-side failure is replayed with exponential backoff unless it names a
> reason that replaying the identical request cannot change.

## Where

| Piece | Role |
| --- | --- |
| `runtime/common.py` `FAIL_FAST_MODEL_HTTP_CODES`, `FAIL_FAST_MODEL_ERROR_RES`, `is_fatal_model_error()` | the short list of "resending cannot help" |
| `runtime/runtime.py` `_is_retryable_model_error(exc, body)` | the single predicate both entry points ask (`infer`, `infer_stream`) |
| `MODEL_API_MAX_RETRIES`, `MODEL_API_RETRY_DELAY` | bounds the cost of the default being wrong |

Both failure points of a round use it: the send (before any token arrived) and the
stream read (after partial output). Replaying after partial output stays safe
because a round's tool calls run only once its stream ended normally, and the
aborted attempt's deltas are dropped with a discard marker frame.

## What stops the loop

**Fatal -- the round ends and the provider's words are reported.**

- Status `401, 403, 404, 405, 406, 410, 422`: wrong key, no permission, no such
  model or endpoint, wrong method, schema-rejected body.
- Wording, whatever the status: rejected/expired credentials, model not found,
  no credit (`insufficient balance`, `insufficient_quota`, "check your plan and
  billing"), malformed request (`invalid parameter`, `field required`,
  `invalid_request_error`), content-policy refusals.
- The turn states why the backoff queue was skipped, so the absence of retries is
  visible rather than inferred:

  ```
  Error: HTTP 401: Unauthorized | body: {"error":{"message":"Incorrect API key provided"}}
  The endpoint named a reason that replaying the request cannot change, so this was not retried.
  ```

Order of checks: a `5xx` (or `408`/`425`) is transient whatever its body says -- an
nginx page served as 502 can contain the words "403 Forbidden" and must not end a
round the way a real refusal would. For every other status the wording is weighed
before it, because providers put billing failures behind a 429, and a bare 429
otherwise means exactly "back off and come back". That is why `Rate limit reached,
slow down` (429) is replayed three times while `You exceeded your current quota,
check your plan and billing details` (429) costs one send.

**Not fatal, and not a resend either -- another remedy owns it.** A rejection that
says the payload does not fit the context window (`request exceeds the context
window: prompt (131218 tokens) ... in the context (131072)`, or a bare HTTP 413)
is answered by the context ladder: fold the endpoint's count into this model's
calibration, then compress → compress deeper → offload oversized tool output →
drop oldest turns, and re-send the *smaller* request. Re-sending the same bytes is
the one thing the endpoint just told us will not work, so the overflow rejection
bypasses the backoff queue entirely and is not counted against it. See
[plan-context-overflow-self-healing.md](plan-context-overflow-self-healing.md).

**Everything else is replayed**, including exceptions this code never anticipated:
a half-parsed provider payload, a `None` where a field was expected, a decoder
that raised, `http.client` truncations, dead sockets, 5xx, 408/409/425/429/599.

## Empty model responses

A 200 whose SSE/NDJSON body ends without content, thinking, or a tool call was
already treated as a transient pre-output failure rather than a successful empty
turn. It stays that way, and the message now says how many sends were made:

```
Error: model API returned an empty response after 3 attempt(s).
```

so the operator can see the backoff happened (and how to change it) instead of
reading it as a single unanswered call.

## Budgets and boundaries

- `MODEL_API_MAX_RETRIES` (default 2) is per model round; an accepted round resets
  it, so a long tool-heavy generation does not spend one round's bad luck on the
  next. `0` switches replaying off; fatal reasons ignore it and end at once.
- A round that already outlived `MODEL_INFER_TIMEOUT` is not replayed: that value
  is the operator's wall-clock bound for the round, and a retry would break it. A
  stall that ended *inside* the bound is replayed.
- User cancellation is honoured during the backoff wait (`Error: user interrupted.`).
- Detection is wording-based, so it is extensible: a new provider phrasing that
  demonstrably cannot be fixed by resending belongs in
  `FAIL_FAST_MODEL_ERROR_RES`; anything not listed gets the benefit of the doubt.
- Not covered: an error object carried *inside* a 200 stream (no provider in use
  here does that; such a round produces no output and is therefore replayed, and
  if the reason was size the context guard still catches the next round).

## Tests

`tests/test_model_error_retry_policy.py` -- the fail-fast wording/status matrix in
both directions, an unexplained 400 and an unparsable provider payload being
replayed, a rejected credential / unknown model costing one send, 429 splitting
into rate-limit vs no-credit, an oversized rejection skipping the backoff queue
(and still being repaired with `MODEL_API_MAX_RETRIES=0`), the empty-response
message naming its attempt count, and the non-streaming `infer` following the same
rule. `tests/test_infer_stream.py::test_infer_stream_openai_empty_stream_is_error`
carries the new message.
