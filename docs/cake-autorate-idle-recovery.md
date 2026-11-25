# CAKE autorate idle-recovery verification

`cake-autorate` raises a reduced shaper by about 1% per adjustment second, but
truncates every step using integer arithmetic: `(rate * 1010) / 1000`. With
the multidial bounds `min=2880` and `base=50000` kbit/s, exact recovery takes
289 whole seconds. Its upstream default of 60 seconds therefore allowed a quiet
dial to enter `IDLE` and retain a few-Mbit CAKE bandwidth indefinitely.

The provisioner now explicitly retains `enable_sleep_function=1`, while setting
each section's `sustained_idle_sleep_thr_s` to `420.0`. The decimal literal is
intentional: the target cake-autorate UCI schema validates this field as a
`float` and rejects the numeric-equivalent integer text `420`. The value is the greater of the
computed min-to-base recovery time plus a 120-second safety margin and a
420-second operational floor. The provisioner preserves a decimal float literal
even if future min/base bounds require a value greater than 420. It does not alter mwan3, interfaces, SQM qdisc
lifecycle, or the reflector list.

## Real-router acceptance

Copy the code through the normal application deployment path first. Do not use
network reload, `ifup`/`ifdown`, mwan3 restart, SQM restart, qdisc deletion, or
synthetic traffic as part of the check.

The test is intentionally a shell integration test rather than a mocked unit
test. It reads the router's actual UCI sections, generated
`config.dialN.sh` files, `cake-autorate` processes, links, byte counters, and
`tc -s qdisc` output. It contains no credentials. Run it on the selected router
or transmit it over the operator's existing SSH access:

```sh
sh tests/live/cake_autorate_idle_check.sh --devices "dial0 dial1 dial2 dial3 dial5 dial6"
sh tests/live/cake_autorate_idle_check.sh --devices "dial0 dial1 dial2 dial3 dial5 dial6" --before /tmp/cake-before.txt
# deploy the UCI-only application change through the approved seamless path
sh tests/live/cake_autorate_idle_check.sh --devices "dial0 dial1 dial2 dial3 dial5 dial6" --after /tmp/cake-before.txt
```

For the strict idle proof, select a period whose real directional throughput is
below each section's `connection_active_thr_kbps`, then run:

```sh
sh tests/live/cake_autorate_idle_check.sh --devices "dial0 dial1 dial2 dial3 dial5 dial6" --watch-idle 420
```

The strict observation uses the same directional counter path as
cake-autorate: download is `ifb4dialN` TX and upload is `dialN` TX. At each
poll it derives actual kbit/s from the counter delta and elapsed time, then
compares both directions to that dial's real UCI
`connection_active_thr_kbps`. Probe/background packets are therefore allowed
when they remain below the active threshold. It exits `3` as
**INCONCLUSIVE** only when a direction reaches that threshold (or a counter
resets); it never manufactures traffic. Otherwise it reports **GREEN** only
when the explicit UCI policy, generated config, processes, interfaces, and both
CAKE qdiscs all exist, and after `--watch-idle` both qdiscs are at their actual
base rates. Any missing/misconfigured invariant is **RED** and exits nonzero.
The optional `--before` file is local evidence only; default mode is entirely
read-only.
