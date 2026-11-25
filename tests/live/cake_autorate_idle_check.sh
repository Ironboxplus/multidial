#!/bin/sh
# OpenWrt live integration check for multidial's cake-autorate idle policy.
#
# This script deliberately uses only live router reads: uci, generated
# config.dialN.sh files, ps, ip, sysfs counters, and tc -s qdisc.  It never
# reloads networking, changes UCI, restarts services, changes qdiscs, or sends
# traffic.  No credentials are embedded; run it locally on the router or pipe
# it to an explicitly selected SSH target.

set -u

CONFIG_DIR=/root/cake-autorate
DEVICES=
MODE=inspect
STATE_FILE=
WATCH_IDLE_S=0
POLL_S=5

usage() {
    cat <<'EOF'
Usage: cake_autorate_idle_check.sh [options]

  --devices "dial0 dial1"  Devices to check (default: cake-autorate UCI sections)
  --config-dir DIR         Generated cake-autorate config directory
  --before FILE            Save an observation snapshot locally, after inspection
  --after FILE             Compare the current observation with a --before snapshot
  --watch-idle SECONDS     Require both real directional rates to remain below
                           connection_active_thr_kbps, then require CAKE at BASE
  --poll-seconds SECONDS   Poll interval for --watch-idle (default: 5)
  -h, --help               Show this help

Default mode is read-only.  --before only writes the named local evidence file;
it does not modify router network state.
EOF
}

die() { echo "ERROR: $*" >&2; exit 2; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --devices) [ "$#" -ge 2 ] || die "--devices needs a value"; DEVICES=$2; shift 2 ;;
        --config-dir) [ "$#" -ge 2 ] || die "--config-dir needs a value"; CONFIG_DIR=$2; shift 2 ;;
        --before) [ "$#" -ge 2 ] || die "--before needs a file"; MODE=before; STATE_FILE=$2; shift 2 ;;
        --after) [ "$#" -ge 2 ] || die "--after needs a file"; MODE=after; STATE_FILE=$2; shift 2 ;;
        --watch-idle) [ "$#" -ge 2 ] || die "--watch-idle needs seconds"; WATCH_IDLE_S=$2; shift 2 ;;
        --poll-seconds) [ "$#" -ge 2 ] || die "--poll-seconds needs seconds"; POLL_S=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

case "$WATCH_IDLE_S:$POLL_S" in
    *[!0-9:]*|:) die "idle/poll values must be non-negative integers" ;;
esac
[ "$POLL_S" -gt 0 ] || die "--poll-seconds must be positive"

if [ -z "$DEVICES" ]; then
    DEVICES=$(uci -q show cake-autorate 2>/dev/null | sed -n 's/^cake-autorate\.\([^.=]*\)=cake_autorate$/\1/p')
fi
DEVICES=$(printf '%s\n' "$DEVICES" | tr ',' ' ')
[ -n "$DEVICES" ] || die "no cake-autorate sections found; pass --devices if needed"

read_counter() {
    cat "/sys/class/net/$1/statistics/$2" 2>/dev/null || printf '%s' '?'
}

traffic_sample() {
    # Match cake-autorate's directional accounting path: ingress/download is
    # the IFB's TX counter, while upload is the dial's TX counter.  Do not use
    # "no packets at all": fping/mwan3/background packets are normal and do
    # not make a connection active under cake-autorate's throughput threshold.
    for dev in $DEVICES; do
        dl_tx=$(read_counter "ifb4$dev" tx_bytes)
        ul_tx=$(read_counter "$dev" tx_bytes)
        threshold=$(uci -q get "cake-autorate.$dev.connection_active_thr_kbps" 2>/dev/null || true)
        case "$dl_tx:$ul_tx:$threshold" in
            *[!0-9:]*|*::*)
                echo "ERROR: cannot read numeric live counters/connection_active_thr_kbps for $dev" >&2
                return 2
                ;;
        esac
        [ "$threshold" -gt 0 ] || {
            echo "ERROR: connection_active_thr_kbps must be positive for $dev" >&2
            return 2
        }
        # device dl_tx ul_tx threshold_kbps
        printf '%s %s %s %s\n' "$dev" "$dl_tx" "$ul_tx" "$threshold"
    done
}

idle_watch() {
    [ "$WATCH_IDLE_S" -gt 0 ] || return 0
    echo "LIVE: requiring $WATCH_IDLE_S seconds below each real connection_active_thr_kbps (poll ${POLL_S}s)"
    window_start=$(date +%s)
    sample_start=$window_start
    previous=$(traffic_sample) || return 2
    while :; do
        sleep "$POLL_S"
        current=$(traffic_sample) || return 2
        now=$(date +%s)
        interval=$((now - sample_start))
        window_elapsed=$((now - window_start))
        [ "$interval" -gt 0 ] || continue
        active=0
        while IFS=' ' read -r dev dl_tx ul_tx threshold; do
            old=$(printf '%s\n' "$previous" | awk -v dev="$dev" '$1 == dev { print; exit }')
            IFS=' ' read -r old_dev old_dl old_ul old_threshold <<EOF
$old
EOF
            [ -n "${old_threshold:-}" ] || {
                echo "ERROR: missing prior live counter sample for $dev" >&2
                return 2
            }
            dl_delta=$((dl_tx - old_dl))
            ul_delta=$((ul_tx - old_ul))
            if [ "$dl_delta" -lt 0 ] || [ "$ul_delta" -lt 0 ]; then
                echo "INCONCLUSIVE: $dev TX counter reset during observation." >&2
                return 1
            fi
            # Rounded-up kbit/s so an actual rate cannot slip below threshold
            # merely through integer truncation: kbit/s = bytes * 8 / 1000 / s.
            dl_kbps=$(((dl_delta * 8 + interval * 1000 - 1) / (interval * 1000)))
            ul_kbps=$(((ul_delta * 8 + interval * 1000 - 1) / (interval * 1000)))
            echo "LIVE rate $dev: download=${dl_kbps}kbit/s upload=${ul_kbps}kbit/s threshold=${threshold}kbit/s over ${interval}s"
            if [ "$dl_kbps" -ge "$threshold" ] || [ "$ul_kbps" -ge "$threshold" ]; then
                active=1
            fi
        done <<EOF
$current
EOF
        if [ "$active" -ne 0 ]; then
            echo "INCONCLUSIVE: real directional throughput reached connection_active_thr_kbps; no synthetic traffic was sent." >&2
            return 1
        fi
        [ "$window_elapsed" -ge "$WATCH_IDLE_S" ] && return 0
        previous=$current
        sample_start=$now
    done
}

snapshot() {
    for dev in $DEVICES; do
        for side in "$dev" "ifb4$dev"; do
            rate=$(tc -s qdisc show dev "$side" 2>/dev/null | sed -n 's/^qdisc cake .* bandwidth \([^ ]*\).*/\1/p' | head -n 1)
            printf 'RATE\t%s\t%s\t%s\n' "$dev" "$side" "${rate:-MISSING}"
        done
    done
}

rate_kbps() {
    # tc writes forms such as 50Mbit, 50000Kbit, and 100000bit.
    printf '%s\n' "$1" | awk '
        { sub(/bit$/, ""); suffix=substr($0, length($0), 1); value=$0 }
        suffix == "K" { sub(/K$/, "", value); print int(value); exit }
        suffix == "M" { sub(/M$/, "", value); print int(value * 1000); exit }
        suffix == "G" { sub(/G$/, "", value); print int(value * 1000000); exit }
        suffix !~ /^[KMG]$/ { print int(value / 1000); exit }
    '
}

integer_recovery_steps() {
    # Mirror /root/cake-autorate/cake-autorate.sh's +1% recovery exactly:
    # shell integer arithmetic truncates `(rate * 1010) / 1000` on EVERY step.
    # A continuous log estimate undercounts the real wait.  Return nonzero if
    # a tiny rate cannot advance, rather than looping forever.
    awk -v min="$1" -v base="$2" 'BEGIN {
        if (min !~ /^[0-9]+$/ || base !~ /^[0-9]+$/ || min <= 0 || base <= 0) exit 2
        rate = min; steps = 0
        while (rate < base) {
            next_rate = int((rate * 1010) / 1000)
            if (next_rate <= rate) exit 3
            rate = next_rate; steps++
            if (steps > 100000) exit 4
        }
        print steps
    }'
}

float_at_least() {
    # OpenWrt's cake-autorate schema writes sustained_idle_sleep_thr_s as a
    # float (e.g. 420.0).  POSIX `test -lt` only accepts integers, so validate
    # and compare the real UCI values in awk instead.
    awk -v actual="$1" -v required="$2" 'BEGIN {
        decimal = "^[0-9]+([.][0-9]+)?$"
        if (actual !~ decimal || required !~ decimal) exit 2
        exit !((actual + 0) >= (required + 0))
    }'
}

fail=0
check_device() {
    dev=$1
    min=$(uci -q get "cake-autorate.$dev.min_ul_shaper_rate_kbps" 2>/dev/null || true)
    base=$(uci -q get "cake-autorate.$dev.base_ul_shaper_rate_kbps" 2>/dev/null || true)
    min_dl=$(uci -q get "cake-autorate.$dev.min_dl_shaper_rate_kbps" 2>/dev/null || true)
    base_dl=$(uci -q get "cake-autorate.$dev.base_dl_shaper_rate_kbps" 2>/dev/null || true)
    sleep_enabled=$(uci -q get "cake-autorate.$dev.enable_sleep_function" 2>/dev/null || true)
    idle_thr=$(uci -q get "cake-autorate.$dev.sustained_idle_sleep_thr_s" 2>/dev/null || true)
    config="$CONFIG_DIR/config.$dev.sh"
    steps=
    required=
    if [ -n "$min" ] && [ -n "$base" ] && [ -n "$min_dl" ] && [ -n "$base_dl" ]; then
        steps=$(integer_recovery_steps "$min" "$base") || steps=
        dl_steps=$(integer_recovery_steps "$min_dl" "$base_dl") || dl_steps=
        if [ -z "$steps" ] || [ -z "$dl_steps" ]; then
            echo "RED: $dev has shaper bounds that cannot make upstream integer recovery progress" >&2
            fail=1
        else
            [ "$dl_steps" -gt "$steps" ] && steps=$dl_steps
            required=$(awk -v steps="$steps" 'BEGIN {
                value = steps + 120
                if (value < 420) value = 420
                printf "%.1f", value
            }')
        fi
    fi

    echo "--- $dev ---"
    echo "UCI: min_ul=${min:-MISSING} base_ul=${base:-MISSING} min_dl=${min_dl:-MISSING} base_dl=${base_dl:-MISSING} enable_sleep=${sleep_enabled:-MISSING} sustained_idle=${idle_thr:-MISSING} required>=${required:-UNKNOWN}"
    if [ "$sleep_enabled" != 1 ] || [ -z "$required" ] || [ -z "$idle_thr" ] || ! float_at_least "$idle_thr" "$required"; then
        echo "RED: $dev idle policy cannot guarantee min-to-base recovery before sleep" >&2
        fail=1
    else
        echo "GREEN: $dev explicit idle policy covers ${steps}s recovery plus margin"
    fi

    if [ -r "$config" ]; then
        echo "LIVE generated config: $config"
        grep -nE '^(enable_sleep_function|sustained_idle_sleep_thr_s)=' "$config" 2>/dev/null || true
    else
        echo "RED: generated config missing/unreadable: $config" >&2
        fail=1
    fi

    if ps w 2>/dev/null | grep -a "[c]ake-autorate\.sh .*config\.$dev\.sh" >/dev/null 2>&1; then
        echo "GREEN: live cake-autorate process found"
    else
        echo "RED: live cake-autorate process missing" >&2
        fail=1
    fi

    for side in "$dev" "ifb4$dev"; do
        echo "LIVE link $side:"
        ip -details link show dev "$side" 2>&1 || { echo "RED: interface unavailable: $side" >&2; fail=1; }
        echo "LIVE qdisc $side:"
        tc -s qdisc show dev "$side" 2>&1
        if ! tc qdisc show dev "$side" 2>/dev/null | grep -q '^qdisc cake '; then
            echo "RED: CAKE qdisc missing on $side" >&2
            fail=1
        fi
        if [ "$WATCH_IDLE_S" -gt 0 ]; then
            expected_base=$base
            [ "$side" = "ifb4$dev" ] && expected_base=$base_dl
            live_rate=$(tc -s qdisc show dev "$side" 2>/dev/null | sed -n 's/^qdisc cake .* bandwidth \([^ ]*\).*/\1/p' | head -n 1)
            live_kbps=$(rate_kbps "${live_rate:-}")
            if [ -z "$live_kbps" ] || [ "$live_kbps" -ne "$expected_base" ] 2>/dev/null; then
                echo "RED: $side remained ${live_rate:-MISSING}, not base ${expected_base:-MISSING} kbit/s after real idle observation" >&2
                fail=1
            else
                echo "GREEN: $side is at base after real idle observation"
            fi
        fi
        echo "LIVE counters $side: rx=$(read_counter "$side" rx_bytes) tx=$(read_counter "$side" tx_bytes)"
    done
}

idle_watch || exit 3
for dev in $DEVICES; do check_device "$dev"; done

if [ "$MODE" = before ]; then
    { printf '# cake-autorate live snapshot %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')"; snapshot; } > "$STATE_FILE" || die "cannot write $STATE_FILE"
    echo "LIVE: wrote local before snapshot: $STATE_FILE"
elif [ "$MODE" = after ]; then
    [ -r "$STATE_FILE" ] || die "cannot read before snapshot: $STATE_FILE"
    echo "LIVE: before snapshot rates:"
    sed -n '/^RATE/p' "$STATE_FILE"
    echo "LIVE: current rates:"
    snapshot
fi

if [ "$fail" -ne 0 ]; then
    echo "RESULT: RED (real router invariants failed)" >&2
    exit 1
fi
echo "RESULT: GREEN (all checked live invariants hold)"
