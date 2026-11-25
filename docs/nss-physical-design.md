# Physical WAN multi-uplink shaping candidate

This branch implements offline plans and a transition journal for a shared NSS
physical WAN with one IGS IFB. It requires hardware qualification before a
network executor or the normal daemon can activate it.

The existing `python -m multidial.nss profile.json` entry point accepts two
optional physical capacities, `physical_download_kbit` and
`physical_upload_kbit`. Omitting them preserves the previous sum of line caps
and the default reservation. With a physical cap, HTB committed rates are
allocated within that cap, while each line retains its individual ceiling.
The default class retains its configured reservation. A cap too small for
the default and one positive rate per line is rejected.

Upload leaf majors are `100..1fe`; download leaf majors are `200..2fe` (hex).
Ordinary ECM egress QoS tags and the 16-bit IGS tag have distinct semantics.
An uplink can optionally carry its current positive `ifindex` and nonnegative
`generation`. Those identify stale observations; they are not firmware fields.
`offload` is `software` by default or `ecm` for an explicit candidate requiring
private macvlan, mark-aware ECM routing, IGS and verified tags.

## Ordered transitions

```sh
python -m multidial.nss_reconcile --state /tmp/nss-state.json --profile profile.json
python -m multidial.nss_reconcile --state /tmp/nss-state.json --receipt TRANSACTION 0 ok
python -m multidial.nss_reconcile --state /tmp/nss-state.json --receipt TRANSACTION 1 failed
python -m multidial.nss_reconcile --state /tmp/nss-state.json --rollback
```

The journal uses a stable process lock, atomic replacement, fsync and an epoch
token. It retains progress across process restarts, rejects out-of-order/stale
receipts and refuses a newer proposal while an earlier transition is pending.
A failure leaves the next step and previous complete profile intact. Rollback
gets a new token and blocks/drains the union of affected devices before asking
an executor to restore the previous tree. Receipts document an external
executor's result; accepting one is not evidence of NSS hardware behavior.

Transitions block learning, wait for old offloads, prepare/update owned queues,
verify routing/NAT and both queue directions, then allow new learning. A lease
change affects that line, while physical-tree changes affect all its lines.
No network commands are executed by this CLI. An executable hardware backend,
live observation/receipt verification and automatic daemon activation are
pending. Operator receipts must not be substituted for those requirements.

## Rate changes

Generated autorate fragments carry direction-specific leaf, ceiling and
committed-rate settings. The Bash adapter enforces the cap at the actual tc
process boundary. A failed command keeps the last successful controller rate.
Fragments missing a required ceiling fail before writing. New fragments must
be generated when upgrading an old manual configuration.

If an executor supplies `md_nss_lease_file` and `md_nss_lease_generation`, the
adapter holds a shared lock on the stable `.lock` file while checking the token
and invoking tc. The publisher/revoker must use an exclusive flock on that same
file. Token publication/revocation is not yet wired to an executor.

## Evidence and remaining firmware work

The Linux CI network job sends real UDP round trips from an external client
namespace through two private macvlans, policy routing and masquerade to an
external upstream namespace. A netdev egress hook independently checks the
physical interface's address, mark and priority. The address-change case renews
the policy routes as well as the lease and checks the other line afterwards.
Linux HTB/FQ-CoDel counters are also observed. This tests the Linux contract,
not the NSS firmware or IGS. It uses a local policy-routing fixture, not the
actual OpenWrt netifd/mwan3 services.

Rate-boundary tests execute Bash and a recording executable to inject tc
success/failure. Transition tests use actual CLI processes and files, including
concurrent proposals. These boundary tests do not replace packet tests.

Before hardware integration, the pinned ECM build needs its existing
`ECM_INTERFACE_SKIP_ACCEL_ENABLE` capability and an IGS tag readiness check.
The current IGS gate checks conntrack state but not the expected managed tag.
The IGS mapping lookup is internal to nss-clients, so a correct readiness check
needs a lifetime-safe mapping/tag interface instead of reaching into an
unexported list or hardcoding queue handles. Kernel build and hardware tests
are pending. `mwan3_enable` alone neither revokes old flows nor handles a
health-check-only failure. Reuse ECM's per-interface defunct mechanism and
await its acknowledgements rather than restarting ECM and flushing all CT.

References: [ECM IGS gate](https://github.com/CodeLinaro-mirror/qsdk_oss_lklm_qca-nss-ecm/blob/8c7355b/frontends/nss/ecm_nss_common.h),
[ECM interface lifecycle](https://github.com/CodeLinaro-mirror/qsdk_oss_lklm_qca-nss-ecm/blob/8c7355b/ecm_interface.c),
[NSS IFB mapping](https://github.com/CodeLinaro-mirror/qsdk_oss_lklm_nss-clients/blob/51be82d/nss_qdisc/igs/nss_ifb.c).
