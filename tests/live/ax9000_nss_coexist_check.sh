#!/bin/sh
# Real-router check that per-dial CAKE and the Qualcomm NSS data plane coexist
# (AX9000 / ipq807x with qca-nss-ecm). Read-only: it never changes a qdisc,
# ECM or a dial.
#
# Static checks (any failure exits 1):
#   - every dial is a vepa (non-private) macvlan, so ECM never offloads it;
#   - no NSS qdisc on the physical WAN or its ifb@ (nss-zk.qos would shape all
#     dials as one aggregate under the per-dial CAKE);
#   - each dial has CAKE on dialN and on its ingress ifb4dialN.
#
# Then, for SECONDS, it samples while the operator pushes real traffic through
# the dials (a speed test from a LAN client): ECM connection count, the
# no-offload exception counter (the ax9000-immortalwrt-build ECM patch counts
# skipped macvlan packets there; without the patch it stays flat and every
# packet pays a failed connection setup instead), softirq share per CPU, and
# bytes per dial.
#
#   sh tests/live/ax9000_nss_coexist_check.sh [SECONDS]   (default 30)
set -u

SECS=${1:-30}
PHY=$(uci -q get multidial.globals.phy_ifname || echo wan)
DIALS=$(ls /sys/class/net | grep '^dial[0-9]*$')
fail=0
bad() { echo "FAIL: $*"; fail=1; }

[ -d /sys/module/ecm ] || echo "note: ECM not loaded (nothing offloads at all)"
[ -n "$DIALS" ] || { echo "no dialN devices"; exit 1; }

for d in $DIALS; do
  mode=$(ip -o -d link show dev "$d" | sed -n 's/.* macvlan mode \([a-z]*\).*/\1/p')
  [ "$mode" = private ] && bad "$d is a private macvlan (ECM would offload it past CAKE)"
  tc qdisc show dev "$d" | grep -q '^qdisc cake' || bad "$d has no CAKE (egress)"
  tc qdisc show dev "ifb4$d" 2>/dev/null | grep -q '^qdisc cake' || bad "ifb4$d has no CAKE (ingress)"
  echo "$d: macvlan mode ${mode:-?}"
done
for dev in "$PHY" "ifb@$PHY"; do
  tc qdisc show dev "$dev" 2>/dev/null | grep -q '^qdisc nss' && bad "NSS qdisc on $dev"
done

ECMDB=/sys/kernel/debug/ecm/ecm_db/connection_count
EXC=/sys/kernel/debug/ecm/stats/ecm_v4_exception_stats
# "<tab><tab> v4_route_out_iff_offload_disabled<pad>:  <count>"
noff() { awk '$1 == "v4_route_out_iff_offload_disabled" {print $3; f=1} END {if (!f) print 0}' "$EXC" 2>/dev/null || echo 0; }
softirq() { awk '/^cpu[0-9]/ {t=0; for (i=2;i<=NF;i++) t+=$i; print $1, $8, t}' /proc/stat; }
bytes() { for d in $DIALS; do echo "$d $(cat /sys/class/net/$d/statistics/rx_bytes) $(cat /sys/class/net/$d/statistics/tx_bytes)"; done; }

echo "sampling ${SECS}s -- run traffic through the dials now"
c0=$(cat $ECMDB 2>/dev/null || echo -); n0=$(noff); s0=$(softirq); b0=$(bytes)
sleep "$SECS"
c1=$(cat $ECMDB 2>/dev/null || echo -); n1=$(noff); s1=$(softirq); b1=$(bytes)

echo "ECM connections: $c0 -> $c1"
echo "ECM no-offload exceptions: +$((n1 - n0))"
echo "$s0" > /tmp/.coexist_s0; echo "$s1" | while read -r cpu si t; do
  set -- $(grep "^$cpu " /tmp/.coexist_s0)
  dt=$((t - $3)); [ $dt -gt 0 ] && echo "$cpu softirq $(( (si - $2) * 100 / dt ))%"
done
rm -f /tmp/.coexist_s0
echo "$b0" > /tmp/.coexist_b0; echo "$b1" | while read -r d rx tx; do
  set -- $(grep "^$d " /tmp/.coexist_b0)
  echo "$d rx $(( (rx - $2) * 8 / SECS / 1000 )) kbit/s, tx $(( (tx - $3) * 8 / SECS / 1000 )) kbit/s"
done
rm -f /tmp/.coexist_b0

[ $fail -eq 0 ] && echo PASS || echo "FAIL (see above)"
exit $fail
