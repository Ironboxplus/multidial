#!/bin/sh
# Real-router check that mwan3 sticky actually sticks (needs mwan3 running with
# every dial online; read-mostly, but "pin" opens connections from a throwaway
# network namespace).
#
#   chains  for every rule with sticky=1, the set of interfaces that have a
#           restore rule in mwan3_rule_<rule> must equal the set of interfaces
#           in its mwan3_policy_<policy> chain (what 17-multidial-sticky keeps
#           true; without it the restore rules are missing and every new
#           connection is balanced afresh).
#   pin     a LAN client (netns bridged into br-lan, 192.0.2.252, outside the
#           DHCP pool) connects $N times to one destination inside a sticky
#           range; every conntrack entry must carry the same dial mark.
#   all     both (default)
#
#   sh tests/live/mwan3_sticky_check.sh [chains|pin|all]
# Exit 0 = pass, 1 = fail. The netns, veth and temp files are removed on exit.
set -u

MODE=${1:-all}
case "$MODE" in
  chains|pin|all) ;;
  *) echo "usage: $0 chains|pin|all" >&2; exit 64 ;;
esac
NS=mdst; VH=mdst0; VN=mdst1; IP=192.0.2.252; GW=192.0.2.1
DST=${DST:-10.90.162.5}   # inside md_svc's 10.90.0.0/16
PORT=${PORT:-443}
N=${N:-10}
CLI=/tmp/mdst_client.py
FAIL=0

cleanup() {
  ip link del "$VH" 2>/dev/null
  ip netns del "$NS" 2>/dev/null
  rm -f "$CLI"
}
trap cleanup EXIT INT TERM

fail() { echo "FAIL: $*"; FAIL=1; }

# mark (id << 8 under mmx_mask 0x3f00) of an interface, from its mwan3_iface_in chain
iface_mark() {
  iptables -t mangle -S "mwan3_iface_in_$1" 2>/dev/null \
    | sed -n "s/.*--comment $1 -j MARK --set-xmark \(0x[0-9a-fA-F]*\)\/.*/\1/p" | head -1
}

check_chains() {
  rules=$(uci show mwan3 | awk -F'[.=]' '
    $3 == "sticky" && $4 == "\0471\047" { st[$2] = 1 }
    $3 == "use_policy" { gsub(/\047/, "", $4); pol[$2] = $4 }
    END { for (r in st) if (r in pol) print r, pol[r] }')
  [ -n "$rules" ] || { fail "no rule with sticky=1 in /etc/config/mwan3"; return; }
  ifaces=$(ls "${MWAN3TRACK_DIR:-/var/run/mwan3track}" 2>/dev/null)
  echo "$rules" | while read -r rule policy; do
    online=$(iptables -t mangle -S "mwan3_policy_$policy" 2>/dev/null \
      | sed -n 's/.*--comment "\([^ ]*\) [0-9]* [0-9]*".*/\1/p' | sort -u | tr '\n' ' ')
    marks=$(iptables -t mangle -S "mwan3_rule_$rule" 2>/dev/null \
      | sed -n 's/.*-j MARK --set-xmark \(0x[0-9a-fA-F]*\)\/0x[0-9a-fA-F]*$/\1/p' \
      | while read -r m; do [ $(( m )) -ne 0 ] && echo $(( m )); done | sort -u)
    restore=
    for d in $ifaces; do
      m=$(iface_mark "$d")
      [ -n "$m" ] || continue
      echo "$marks" | grep -qx "$(( m ))" && restore="$restore $d"
    done
    restore=$(echo $restore | tr ' ' '\n' | sort -u | tr '\n' ' ')
    if [ -n "$online" ] && [ "$online" = "$restore" ]; then
      echo "OK   $rule: restore rules == online members ($online)"
    else
      echo "FAIL $rule: online=[$online] restore=[$restore]"
      exit 1
    fi
  done || FAIL=1
}

check_pin() {
  mask=$(cat /var/run/mwan3/mmx_mask)
  cat > "$CLI" <<'PYEOF'
import socket, sys
# plain connect(): the router's python3 has no idna codec for getaddrinfo
for _ in range(int(sys.argv[3])):
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((sys.argv[1], int(sys.argv[2])))
    except OSError:
        pass
    s.close()
PYEOF
  ip netns add "$NS"
  ip link add "$VH" type veth peer name "$VN"
  ip link set "$VN" netns "$NS"
  ip link set "$VH" master br-lan up
  ip netns exec "$NS" ip link set lo up
  ip netns exec "$NS" ip addr add "$IP/24" dev "$VN"
  ip netns exec "$NS" ip link set "$VN" up
  ip netns exec "$NS" ip route add default via "$GW"
  sleep 2
  ip netns exec "$NS" ping -c 1 -W 2 "$GW" >/dev/null || { fail "netns cannot reach $GW"; return; }

  ip netns exec "$NS" python3 "$CLI" "$DST" "$PORT" "$N"
  marks=$(conntrack -L -p tcp -s "$IP" -d "$DST" --dport "$PORT" 2>/dev/null \
    | sed -n 's/.*mark=\([0-9]*\).*/\1/p')
  count=$(echo "$marks" | grep -c .)
  uniq_marks=$(echo "$marks" | while read -r m; do [ -n "$m" ] && echo $(( m & mask )); done | sort -u)
  nuniq=$(echo "$uniq_marks" | grep -c .)
  echo "connections seen in conntrack: $count (asked for $N); distinct dial marks: $nuniq ($(echo $uniq_marks))"
  [ "$count" -ge "$N" ] || fail "only $count/$N connections tracked"
  [ "$nuniq" -eq 1 ] || fail "client was spread over $nuniq dials, sticky is not holding"
  ipset list mwan3_rule_ipv4_md_svc 2>/dev/null | grep "^$IP," || fail "no ipset pin for $IP in md_svc"
}

[ "$MODE" = pin ] || check_chains
[ "$MODE" = chains ] || check_pin

[ "$FAIL" -eq 0 ] && echo "PASS" || echo "FAILED"
exit "$FAIL"
