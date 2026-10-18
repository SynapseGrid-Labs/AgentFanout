# Coordinator-backed Local contract

`local_profiles.py` and `coordinator_local.py` provide opt-in contract primitives. They do not enable hosted dispatch or attest a deployment. The native skill remains the default; legacy `LocalEndpointRequestV1` and persisted Local runs retain their meanings.

LocalModelIdentityV1 distinguishes a safe logical selector from namespaced artifact repository and served name. It pins immutable artifact revision/digest/precision, backend/version/config/template, endpoint identity, resource domain, serving epoch, inference profile and evaluation capability. AEON's selected BF16 repository requires BF16. Nemotron's precision must be explicit. No model identifier becomes shell or filesystem authority.

LocalInferenceProfileV1 uses attested backend controls rather than universal temperature/reasoning parameters. `build_inference_request` verifies the identity and profile digests; the profile material digest excludes its reciprocal `identity_sha256` field. Unsupported thinking controls omit the field. Constrained WorkerResultV1/ValidatorResultV1 generation requires `json_schema` conformance evidence and retains downstream structural and semantic validation. A declaration or digest alone is never live conformance. Response parsing requires exact served identity, complete stop, no tool calls, separated attested reasoning and valid token accounting; unknown provider dollars remain unknown.

Deployment supplies a reviewed `FixedClientTransport` immutable invocation and digest. No deployment addresses, credential paths or SSH route are shipped publicly. Worker input cannot change the invocation. Only status, transition/job observation, capability request, identified job cancellation and bounded non-streaming inference are exposed. Administrative admission, global switching, shell commands, artifact download, generic submit and reconcile-absent operations are denied.

Host `authorize_effect` must durably persist authorization and an effect journal before invoking an effect. Inference additionally requires CoordinatorResourceBindingV1 supplied by the coordinator, independently observed current identity and a separate role reservation. `observed_readiness` is an observation gate and cannot create a reservation. Queue waiting must stay outside execution attempts/waves. Epoch/model/config drift, expiry and worker reservations used for validators are refused. Local client timeout or termination never proves remote cancellation. Cancellation lacking effects and accounting reconciliation quarantines the binding; confirmed cancellation cannot reuse the cancelled reservation.

The current deployment needs additional coordinator evidence for exact identity, role reservations, durable text-request correlation and cancellation. Until those exist and live conformance passes, the coordinator profile must remain unavailable. Fixtures prove engineering behavior only.

Rollback stops new admission, drains or reconciles identified effects under the compatible runtime, retains unresolved bindings, preserves ledger history and activates a verified immutable prior release. It never redirects to raw inference ports, fakes readiness, changes AEON precision or removes identity enforcement.

Supplied prompts and returned content/reasoning use the existing sensitive-content screen. Sensitive responses raise LocalSensitiveResponseError without exposing text, retaining only verified numeric usage observations for host failure accounting; unknown provider cost remains unknown and host reservation charges are unchanged. Broker request reconstruction rejects sensitive text before authorization or process spawning.


## Durable coordinator contract ABI

A separately reviewed coordinator candidate adds fixed client operations
`fanout-attest`, `fanout-reserve`, `fanout-reservation`, `fanout-inference`,
`fanout-request`, `fanout-cancel`, and `fanout-release`. These are not claimed to
exist in the installed client. Deployment must attest the exact client interface
and invocation before use; neither a flag nor a ready status proves installation.
The broker validates bounded stdin, allowlisted capabilities, UUID correlation,
exact role/profile/resource evidence and durable host authorization before effects.

The coordinator alone stores role reservations and identified text requests in
its existing state database, uses its existing domain locks and runtime adapters,
and fences lifecycle/legacy inference while reservations or uncertain effects
remain. Restart does not replay prepared/running inference. Running cancellation
is uncertain until exact effects and accounting are positively reconciled; a
local CLI exit never authorizes release. Released and expired reservations cannot
admit inference. Worker and validator reservations are distinct.

Exact installed identity/profile attestations are immutable owner-only evidence,
bound to a fresh observed serving owner and epoch; no caller-authored JSON can
establish them. The coordinator's new routes remain disabled and its candidate
is private. The host-side translation is observation, never a shadow registry of
ownership. The opt-in hosted bridge implements the coordinator receipt adapter; this ABI
does not prove installed model/profile conformance. Existing Local receipts retain their
legacy meanings; they must not be fabricated from these new reservation fields.


## Hosted coordinator receipts

`CoordinatorHostedBoundaryV1` is carried additively by the host worker launch,
validator attempt start, and validator launch receipts. It binds the coordinator's
separate role reservation, fresh owner/fence/epoch observation, exact Local identity
and inference profile, installed controls and role schema digest, bounded payload
digest, adapter identity, and a fresh durable request UUID. Preparation creates no
authorizing receipt and performs no inference or lifecycle action.

Only the profiled host dispatcher can verify this boundary against its frozen
assignment, numeric reservation, egress policy and durable session guard. Legacy
Local endpoint/sandbox receipts cannot satisfy it. Hosts must inject independently
proved conformance, fixed transport, capability observation and durable effect
authorization/journal callbacks. No production callback or transport is chosen by
this module. Validator requests use the previously filtered peer packet, a distinct
reservation and request ID, and a host start receipt committed before dispatch.

Polling and cancellation use identified coordinator requests. Fixed host transport
calls must have a proved timeout of at most ten seconds; the execution deadline
and three additional call allowances must fit inside the observed role lease. A timeout, CLI exit,
or cancellation acknowledgment never sets the host termination signal. Unknown
remote effects, rejected identity/content or incomplete token semantics retain
numeric host reservations and block replay/terminal completion. Reconciliation
journals bounded identity/correlation hashes and observed numeric tokens, never raw
provider content or reasoning. Missing cache/reasoning measurements are refused,
not filled with zero. Provider dollar cost remains unknown; accepted executions use
the existing numeric worker and validator reservation accounting.

Reader/writer compatibility is additive inside the pinned hosted envelope. The new
writer emits the versioned boundary only for `coordinator_local`; old runtimes lack
its verifier and refuse Local admission. Active runs are not upgraded in place.
Rollback requires the compatible runtime to reconcile exact requests/reservations;
no history is rewritten and an old reader is never used to resume new effects.
