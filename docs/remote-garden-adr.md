# Remote Pico Garden: transport and trust boundary

Status: proposed architecture for [#90](https://github.com/dhuzard/pico-orchestrator/issues/90), R0. Reviewed against the local Garden implementation on 2026-10-30. This decision does **not** enable remote access.

## Decision

Keep `pico watch` bound to `127.0.0.1` by default. Build remote access as a separate, explicitly enabled gateway that accepts only authenticated private HTTPS connections, provides a read-only role first, and forwards a narrow, sanitized Garden API. Prefer a private overlay with HTTPS (Tailscale Serve is one candidate) for the first pilot. A deliberate SSH tunnel can be supported for a single operator, but the tunnel alone does not provide Garden user or device authorization. Do not use Tailscale Funnel, public port forwarding, `0.0.0.0` on Garden, or a copied local Garden URL as a shortcut.

The first remotely available release should be **read-only**. Controls need a separate implementation and security review. In particular, the existing local `X-Pico-Token` and URL fragment are session capabilities, not proof that a specific human approved a mutation. The remote gateway must never turn either into a controller identity.

## Why a separate gateway

Today `tools/watch.py` binds to loopback, accepts only a loopback `Host`, and `tools/garden_controls.py` requires a loopback `Origin` plus a per-process token for actions. With controls enabled, data endpoints also require that token. Mutating actions use a preview and state-bound confirmation, and `garden_actions` records their outcomes. These checks are designed for same-host use. Changing only the bind address or proxying arbitrary requests would either fail the checks or dilute their security meaning.

The gateway would terminate remote access **before** Garden. It must not expose a general reverse proxy to `/api/action`, `/api/setup`, static pages that embed authority, or future endpoints by default. It should call a defined read-only adapter with an explicit field allowlist. Only after the control design is implemented may it forward a separately authorized action request to the existing server-side action service. The planner, scheduler, autonomy policy, repository identity checks, preview hash and state recheck remain authoritative.

```text
Android browser ── private HTTPS + device/user session ── gateway
                                                        │ allowlisted read API
                                                        ▼
                                                 Garden state services
                                                        │
                                                        ▼
                                               local workspace/run records

Future control path: gateway role check → action preview → deliberate human
confirmation → state-bound action service → audit/provenance.
```

The trust boundary is at the gateway. The private network authenticates transport or device membership; it does not establish that the current browser user is authorized to approve a repository mutation. The gateway must independently bind an operator identity, enrolled device, role, and short-lived browser session to each request. The local Garden token stays local.

## Transport options

| Option | Fit | Required limits |
| --- | --- | --- |
| Private overlay with HTTPS, for example Tailscale Serve | Preferred first pilot: a phone can reach a named private endpoint across Wi-Fi and mobile data without opening a public inbound port. Serve is private to the tailnet; Funnel is a different, public feature. | Explicit opt-in; approved tailnet ACL and device enrollment; HTTPS verified end to end to the gateway; gateway user/device session and role checks still required. Do not accept identity headers supplied by an arbitrary client. |
| SSH local forwarding through a private authenticated host | Useful for a technical single-operator fallback and for comparing transport behavior. | Bind tunnel endpoints to loopback, verify host identity, avoid shared credentials, and still authenticate the browser to the gateway. Document the extra phone setup and tunnel lifetime. |
| Public reverse proxy, Funnel, or port forwarding | Outside this milestone. | A private Garden interface is not made safe by a public URL or TLS alone. A public design would require a separate threat model and explicit approval. |

The selected proxy must have a single documented identity source. If it passes an identity header, the gateway accepts that header only over a protected local hop from the configured proxy, strips client-supplied copies, and verifies the proxy's configuration and source. A safer first implementation is to ignore proxy identity headers and perform gateway-managed enrollment and authentication. Never trust `X-Forwarded-*` values from the browser to decide host, scheme, origin, or identity.

## WSL2 and availability

On WSL2, Windows-to-WSL localhost forwarding commonly makes a Linux loopback service reachable from Windows; networking modes and VPN behavior vary. The pilot must verify the actual Windows/WSL path before configuring Serve or an SSH tunnel, with Garden still listening on Linux loopback. No implicit change to Windows firewall, WSL networking mode, DNS, or machine-wide tunnel settings is authorized by opening Garden. The workstation, WSL instance, Garden process, gateway, and overlay/tunnel must all remain running. If any component stops, the phone displays **offline** with the last successful update time; it must not claim that PICO is still operating or queue actions for later replay.

## Roles, enrollment, and revocation

The remote mode is disabled at startup unless the operator explicitly enables a reviewed gateway configuration. Enrollment must be initiated locally by the operator, bind a named device and user, expire promptly, and be confirmed on the host. A QR code or link may carry only a short-lived enrollment challenge; possession alone cannot confer a control role. Sessions should use secure, HttpOnly, SameSite cookies bound to the approved HTTPS origin, with server-side expiry and revocation. The default role is `monitor`, allowed only sanitized reads. A later `controller` role receives a separately granted, bounded action set per workspace or repository; neither role implies `push_branch`, `create_pr`, merge, deploy, or policy edits. A lost device must be revocable without changing repository policy, and disabling remote mode invalidates all gateway sessions.

## Threats and required controls

| Threat or failure | Required behavior |
| --- | --- |
| Copied local link, fragment, QR code, token or stolen browser session | Link and enrollment challenge do not authorize actions. Require enrolled identity, active session, role, fresh deliberate confirmation and action-specific server check. Revoke sessions/devices; keep the local token out of remote responses, URLs and logs. |
| Cross-site request, DNS rebinding, forged proxy headers | Exact approved HTTPS `Host` and `Origin` on all remote API requests; CSRF token for state-changing requests; reject missing or unexpected origins; strict cookie attributes; trusted proxy allowlist and header stripping. Keep current loopback Host/Origin checks intact. |
| Replay, double tap, reconnect or stale preview | Short-lived, single-use confirmation bound to actor, device, session, target, action, repository, run/attempt and current state/plan hash. Serialize mutations at the existing action boundary; reject reused or stale confirmation. Never queue a mutation while offline. |
| Stolen phone or shared tailnet membership | Device and session revocation, short expiry, role scoping, visible active devices, no implicit controller access from tailnet membership. |
| Untrusted issue body, task text or worker instruction | Render as text, never as authority. Server-side action allowlist and autonomy policy decide mutations. No arbitrary command or live worker prompt injection endpoint. |
| Sensitive data in transport or audit | Return only the established display fields and minimum decision metadata. No raw prompts, traces, secrets, checkout contents or tokens. Record actor/device, time, repository/workspace/task/run/attempt, requested scope, preview digest and result; redact credentials and sensitive text. |

## Delivery gates

1. **R1, read-only:** explicit enable/disable and enrollment, private HTTPS, monitor role, a mobile task/attention view using sanitized Garden state, connected/reconnecting/offline/stale indicators and last successful update. Prove read-only devices cannot call action or setup endpoints. Test Linux and WSL2 transport separately; perform an Android Chrome pilot on Wi-Fi and mobile data. Record host-awake and offline behavior.
2. **R2, controls:** define the controller permission contract and supported human checkpoint before exposing any action. Add action availability from the server, exact effect preview, actor-bound single-use confirmation, repository policy checks, audit and duplicate/replay protection. Test cancellation and pause dispatch separately from pausing browser refresh. A decision or bounded guidance applies only at a supported checkpoint/new attempt, not an executing worker.
3. **R3, polish:** accessibility and reconnect tests, optional installability and notifications. Notifications, if added, need opt-in and minimal payloads; polling remains functional without them.

Security tests must cover unauthorized, unpaired, expired and revoked devices; wrong Host/Origin; CSRF; forged proxy headers; stale preview; duplicate POST; concurrent tabs; browser reload; network loss; and a changed repository or run between preview and confirm. Real-device and external tunnel tests are release evidence, not substitutes for deterministic local tests. Until those gates pass, the supported remote procedure is to use an independently secured desktop session on the workstation, not to expose Garden directly.

## Sources for transport assumptions

- [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) and [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel): private tailnet serving versus public internet exposure.
- [Microsoft WSL networking](https://learn.microsoft.com/windows/wsl/networking): Windows-to-WSL localhost behavior and networking modes. Validate the actual host configuration during the pilot.
