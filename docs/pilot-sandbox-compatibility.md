# Pilot: Claude sandbox compatibility

The 16 November 2026 pilot on Ubuntu 24.04 ran Claude Code 2.1.318 as `pico`
with bubblewrap 0.9.0. The outer bubblewrap namespace probe passed, but Bash
failed before its command ran: Claude's bundled `apply-seccomp` failed writing
`/proc/self/setgroups`. The host's enforcing `bwrap-userns-restrict` profile
transitions bubblewrap children into `unpriv_bwrap`, which denies capabilities;
a contemporaneous AppArmor audit recorded a `sys_admin` denial. A nested
`unshare` check also fails there, but is an approximation of the helper, not
proof of its behavior. PICO now treats that check as a conservative
prerequisite: a runtime that does not need nested mappings may be refused too.
It requires `unshare` from util-linux, in addition to bubblewrap and socat.
Passing it is not a complete sandbox validation.

## Reproducing the bundled helper without a model

This diagnostic is specific to the pilot's **native Linux x86-64** executable,
whose SHA-256 was `708c77b204db30086ff6f1ed34577bda03e1be74ced80557cbf7b54249ebf0bd`.
Its embedded code identifies `ARGV0=apply-seccomp` as the multicall helper
entry point. Do not run this invocation against an unverified executable:
other versions or wrappers may interpret the arguments differently. Run as
the service account, under the service's actual security context, with the
same bubblewrap and without credentials in the probe environment. The worker
executable is represented below by an absolute, operator-verified path.

```sh
sha256sum /absolute/path/to/claude
# Continue only if the digest matches the pilot digest above.
env -i PATH=/usr/bin:/bin /usr/bin/bwrap \
  --ro-bind / / --dev /dev --proc /proc \
  --unshare-user --unshare-pid --unshare-net --die-with-parent \
  /usr/bin/env ARGV0=apply-seccomp /absolute/path/to/claude /bin/true
```

On the pilot, this returned the same `apply-seccomp: write
/proc/self/setgroups ... Permission denied` error seen in the worker. The
binary is the same inode as the installed native package binary. This test
exercises the bundled helper without a Claude model invocation, but its
minimal mount setup and absence of the network proxy do not reproduce every
runtime setting or establish complete sandbox containment.

For comparison, the official npm native x86-64 package for Claude Code 2.1.287
was downloaded and its SHA-512 checked against npm registry metadata. Its
extracted executable had SHA-256
`3920489a5109cff5786a1a392c25277408ff22bc796d5edb9c16a60e5a1718f0`;
the same helper invocation returned the same error. This is not evidence that
all earlier versions fail or that downgrading is safe. A temporary extracted
Ubuntu bubblewrap 0.11.1 package also failed to establish the outer network
namespace under the pilot's existing policy; its temporary path was not covered
by the installed `/usr/bin/bwrap` AppArmor attachment.

## Dedicated candidate for review

A disposable candidate was tested using the non-root service account and
`setpriv --no-new-privs` before starting bubblewrap, with `--cap-drop ALL`.
The exact 2.1.318 helper completed. The sandboxed process had the expected
AppArmor label, the service UID, zero effective, permitted and ambient
capabilities, `NoNewPrivs: 1`, and a seccomp filter. A workspace write and
`git init` succeeded. With same-user positive controls on the host, a masked
sentinel read, a protected-directory write, Unix socket creation, and a host
loopback connection were denied. Raw mount, remount, umount, pivot_root,
unshare and setns calls returned EPERM. The generic nested namespace
preflight also passed. The temporary profiles were removed immediately and
their absence checked. This is evidence for this specific setup, not a full
Claude Bash session, network proxy test, or proof against all escapes.

The candidate template is `deploy/apparmor/pico-worker-bwrap`. Unlike the
stock Ubuntu child profile, its confined child permits `sys_admin` capability
checks. The dedicated program path is the scope of this change, not a complete
security boundary: anyone able to execute that binary can supply its own
arguments. In particular, a malicious same-account caller could try to use it
to circumvent the host's unprivileged user namespace restrictions. The helper
executes only after an initial shell and, where used, proxy processes, so the
helper's later seccomp filters do not cover those earlier processes. Review
these implications and restrict access to the dedicated executable before
considering deployment. The template is **not a general AppArmor policy**.

The tested executable was extracted without installation from Ubuntu's
[0.11.1-1ubuntu0.3 amd64 package](https://archive.ubuntu.com/ubuntu/pool/main/b/bubblewrap/bubblewrap_0.11.1-1ubuntu0.3_amd64.deb).
The package SHA-256 was
`d3a6c1b6b0e0474eaed6dbd055c0d601fd875af153976a0dc4aae75f4ea10868`;
the extracted `usr/bin/bwrap` SHA-256 was
`523da3e7399044be5163aee6f57a77a6bef7454376e28f0a0627920bae1b76b6`.
Independently verify package provenance and current security support before
use. This artifact was evaluated on Ubuntu 24.04 x86-64; it is not a
recommendation to install a package from another release system-wide.

A reviewer may stage the executable at the profile's exact path
`/opt/pico-sandbox/bin/bwrap` with root-owned, non-writable parent directories,
mode 0755 and no setuid bit or file capabilities. Restrict traversal to a
dedicated service group, and ensure the worker account cannot modify the
binary, profile, or parent directories. Verify the digest before use. The
existing `/usr/bin/bwrap` profile must remain unchanged. After reviewing the
profile, load it explicitly with `apparmor_parser -a -K` (not `-r`) only when
the profile names are absent, and verify it is enforcing. Set the service's
PATH to put `/opt/pico-sandbox/bin` first only after the validation below;
restart or modify no service as part of staging. The pinned native Claude
binary's bundled runtime code supplies `--cap-drop ALL` and no `--cap-add`
when the service UID is nonzero. That finding is specific to this exact binary;
verify the actual generated invocation and re-review after any update.

With the candidate profile loaded and no production service changed, run as
the non-root service account:

```sh
python3 tools/verify_claude_sandbox_candidate.py --claude /absolute/path/to/claude
```

This no-model diagnostic checks both pinned binary digests before invoking the
native helper. It tests the nested preflight, the exact helper, confinement,
and positive and negative controls. It uses `setpriv --no-new-privs` and
requires Python 3, util-linux, git and the relevant AppArmor profile. A changed
binary fails closed until independently reviewed. Run it in a disposable test
context with no live workload. Its temporary files are removed on exit. A pass
does not prove the complete Claude Code proxy/settings execution path.

For rollback, stop using the dedicated PATH, then remove only this candidate
profile using `apparmor_parser -R -K /path/to/pico-worker-bwrap` and verify
both `pico_worker_bwrap` and `pico_worker_child` are absent from
`/sys/kernel/security/apparmor/profiles`; treat a removal failure or a remaining
profile as a failed rollback. Remove the dedicated files only after no process
is using them. Do not unload or alter the system `bwrap` or `unpriv_bwrap`
profiles. Production deployment and a new model run require separate operator
authorization.
