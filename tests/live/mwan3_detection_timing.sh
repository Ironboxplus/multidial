#!/bin/sh
# Real-router timing of mwan3 dead-dial detection for ONE dial.
#
# Silently drops the ICMP echo REPLIES arriving on DEV from its track targets
# (requests still leave, so ping waits its full timeout, like an uplink the
# campus AC black-holes), measures time until mwan3track marks the dial
# offline (= removed from the balancing policy), then restores and measures
# time back to online. Real traffic is not dropped; the dial only stops
# receiving NEW connections while offline. Rules are always removed on exit.
#
# Do NOT drop the echo REQUESTS in OUTPUT instead: a locally rejected send
# makes ping fail instantly, which measures ~3 s and hides the timeout cost.
#
# Clock: /proc/uptime. busybox `date` has no %N, and printf "%d" overflows on
# a long uptime, hence "%.0f". Polling every 1 s => results are +-1 s; the
# mwan3track lines in `logread` carry independent timestamps to cross-check.
#
#   sh tests/live/mwan3_detection_timing.sh dial3
set -u

DEV=${1:-}
case "$DEV" in
  dial[0-9]*) ;;
  *) echo "usage: $0 dialN" >&2; exit 64 ;;
esac
ST=/var/run/mwan3track/$DEV/STATUS
TARGETS=$(uci -q get mwan3.$DEV.track_ip)
TAG="mdtrack_test_$DEV"
[ -n "$TARGETS" ] || { echo "no mwan3.$DEV.track_ip" >&2; exit 1; }

add() {
  for ip in $TARGETS; do
    iptables -t raw -I PREROUTING -i "$DEV" -p icmp --icmp-type echo-reply \
      -s "$ip" -m comment --comment "$TAG" -j DROP
  done
}
del() {
  while iptables -t raw -S PREROUTING | grep -q "$TAG"; do
    iptables -t raw -D PREROUTING $(iptables -t raw -S PREROUTING | grep "$TAG" \
      | head -1 | sed 's/^-A PREROUTING //')
  done
}
trap del EXIT INT TERM

ms() { awk '{printf "%.0f", $1*1000}' /proc/uptime; }
waitfor() {
  want=$1; start=$2
  while :; do
    s=$(cat "$ST")
    [ "$s" = "$want" ] && { echo $(( $(ms) - start )); return 0; }
    [ $(( $(ms) - start )) -gt 120000 ] && { echo "TIMEOUT(last=$s)"; return 1; }
    sleep 1
  done
}

echo "dev=$DEV timeout=$(uci -q get mwan3.$DEV.timeout)" \
  "interval=$(uci -q get mwan3.$DEV.interval) down=$(uci -q get mwan3.$DEV.down)" \
  "up=$(uci -q get mwan3.$DEV.up) targets=$TARGETS"
[ "$(cat "$ST")" = online ] || { echo "$DEV not online, abort" >&2; exit 1; }

t0=$(ms); add
echo "detect_offline_ms=$(waitfor offline "$t0")"
t1=$(ms); del
echo "recover_online_ms=$(waitfor online "$t1")"
echo "rules_left=$(iptables -t raw -S PREROUTING | grep -c "$TAG")"
