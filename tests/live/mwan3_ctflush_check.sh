#!/bin/sh
# Real-router check: after a dial's conntrack entries are deleted, does a LAN
# client's existing TCP connection fail FAST (server RST via a live dial)
# instead of retransmitting into the dead path?
#
# The client is a throwaway network namespace bridged into br-lan
# (use a free address outside the DHCP pool), so its traffic takes the real LAN
# path: br-lan -> fw4 forward -> mwan3 -> dial -> masquerade. It keeps one
# HTTP/1.1 keep-alive connection to $DST and sends a HEAD every second,
# printing OK / ERR with CLOCK_MONOTONIC stamps (same clock as /proc/uptime).
#
#   manual   delete just this flow's conntrack entry by hand (no mwan3 event):
#            proves the mechanism, incl. that fw4's "ct state invalid drop"
#            on dial* does not eat the loosely re-tracked mid-stream packet.
#   offline  make the client's dial go offline the way the timing script does
#            (drop the track targets' echo replies on it) and let the
#            /etc/mwan3.user hook flush it: proves the deployed path.
#
#   sh tests/live/mwan3_ctflush_check.sh manual|offline
# Everything (netns, veth, raw rules, temp files) is removed on exit.
set -u

MODE=${1:-}
case "$MODE" in
  manual|offline) ;;
  *) echo "usage: $0 manual|offline" >&2; exit 64 ;;
esac
NS=mdct; VH=mdct0; VN=mdct1
: "${IP:?set the unused LAN test address}" "${GW:?set the LAN gateway}" "${DST:?set the test HTTP server IPv4 address}"
TAG=mdct_test
CLI=/tmp/mdct_client.py; LOG=/tmp/mdct_client.log
CPID=; DEV=

cleanup() {
  [ -n "$CPID" ] && kill "$CPID" 2>/dev/null
  while iptables -t raw -S PREROUTING | grep -q "$TAG"; do
    iptables -t raw -D PREROUTING $(iptables -t raw -S PREROUTING | grep "$TAG" \
      | head -1 | sed 's/^-A PREROUTING //')
  done
  ip link del "$VH" 2>/dev/null
  ip netns del "$NS" 2>/dev/null
  rm -f "$CLI" "$LOG"
}
trap cleanup EXIT INT TERM

now() { awk '{print $1}' /proc/uptime; }
ms() { awk '{printf "%.0f", $1*1000}' /proc/uptime; }

if [ "$MODE" = offline ] && ! grep -q mwan3-ctflush /etc/mwan3.user; then
  echo "/etc/mwan3.user has no mwan3-ctflush hook, abort" >&2; exit 1
fi

cat > "$CLI" <<'EOF'
import socket, sys, time
# plain connect(): create_connection -> getaddrinfo needs the idna codec,
# which the router's python3 does not ship
s = socket.socket()
s.settimeout(5)
s.connect((sys.argv[1], 80))
print("%.1f CONNECTED sport=%d" % (time.monotonic(), s.getsockname()[1]), flush=True)
req = b"HEAD / HTTP/1.1\r\nHost: %s\r\nConnection: keep-alive\r\n\r\n" % sys.argv[1].encode("ascii")
for _ in range(int(sys.argv[2])):
    t = time.monotonic()
    try:
        s.sendall(req)
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = s.recv(4096)
            if not d:
                raise EOFError("FIN")
            buf += d
        print("%.1f OK %s %.0fms" % (t, buf.split(b"\r\n", 1)[0].decode(),
                                     (time.monotonic() - t) * 1000), flush=True)
    except Exception as e:
        print("%.1f ERR %s after %.0fms" % (t, type(e).__name__,
                                            (time.monotonic() - t) * 1000), flush=True)
        break
    time.sleep(1)
EOF

ip netns add "$NS"
ip link add "$VH" type veth peer name "$VN"
ip link set "$VN" netns "$NS"
ip link set "$VH" master br-lan up
ip netns exec "$NS" ip link set lo up
ip netns exec "$NS" ip addr add "$IP/24" dev "$VN"
ip netns exec "$NS" ip link set "$VN" up
ip netns exec "$NS" ip route add default via "$GW"
sleep 2
ip netns exec "$NS" ping -c 1 -W 2 "$GW" >/dev/null || { echo "netns cannot reach $GW" >&2; exit 1; }

ip netns exec "$NS" python3 "$CLI" "$DST" 90 > "$LOG" 2>&1 &
CPID=$!
i=0
until grep -q ' OK ' "$LOG" || [ $i -ge 10 ]; do sleep 1; i=$((i+1)); done
grep -q ' OK ' "$LOG" || { echo "client never got a reply" >&2; cat "$LOG"; exit 1; }

mark=$(conntrack -L -p tcp -s "$IP" -d "$DST" --dport 80 2>/dev/null \
  | sed -n 's/.*mark=\([0-9]*\).*/\1/p' | head -1)
mask=$(cat /var/run/mwan3/mmx_mask)
for d in $(ls /var/run/mwan3track); do
  m=$(iptables -t mangle -S "mwan3_iface_in_$d" 2>/dev/null \
    | sed -n "s/.*--comment $d -j MARK --set-xmark \(0x[0-9a-f]*\)\/.*/\1/p")
  [ -n "$m" ] && [ $(( m )) -eq $(( mark & mask )) ] && DEV=$d
done
echo "client flow mark=$mark -> ${DEV:-?}"
[ -n "$DEV" ] || { echo "cannot map mark to a dial" >&2; exit 1; }
sleep 2

if [ "$MODE" = manual ]; then
  echo "$(now) DELETE $(conntrack -D -p tcp -s "$IP" -d "$DST" --dport 80 2>&1 | tail -1)"
else
  ST=/var/run/mwan3track/$DEV/STATUS
  [ "$(cat "$ST")" = online ] || { echo "$DEV not online, abort" >&2; exit 1; }
  for ip in $(uci -q get mwan3.$DEV.track_ip); do
    iptables -t raw -I PREROUTING -i "$DEV" -p icmp --icmp-type echo-reply \
      -s "$ip" -m comment --comment "$TAG" -j DROP
  done
  t0=$(ms)
  echo "$(now) BLACKHOLE track replies on $DEV"
  until [ "$(cat "$ST")" = offline ] || [ $(( $(ms) - t0 )) -gt 60000 ]; do sleep 1; done
  echo "$(now) $DEV STATUS=$(cat "$ST")"
fi

i=0
until grep -q ' ERR ' "$LOG" || [ $i -ge 30 ]; do sleep 1; i=$((i+1)); done
sleep 1
echo "--- client log"
cat "$LOG"
echo "--- fresh connection from the client after that"
ip netns exec "$NS" python3 -c "
import socket, sys, time
t = time.monotonic()
s = socket.socket(); s.settimeout(5); s.connect((sys.argv[1], 80))
print('connect ok %.0fms' % ((time.monotonic() - t) * 1000))" "$DST"
echo "--- new flow now on mark: $(conntrack -L -p tcp -s "$IP" -d "$DST" --dport 80 2>/dev/null \
  | sed -n 's/.*mark=\([0-9]*\).*/\1/p' | tr '\n' ' ')"
[ "$MODE" = offline ] && logread | grep mwan3-ctflush | tail -2
exit 0
