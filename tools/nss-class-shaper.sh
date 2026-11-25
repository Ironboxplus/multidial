#!/usr/bin/env bash
# Source from one cake-autorate instance after setting md_nss_dl_device,
# md_nss_ul_device and md_nss_leaf to the planner's leaf major (e.g. 100).
# Experimental adapter for per-uplink nsshtb classes; the controller still
# measures counters and pings on its own logical uplink.

md_nss_set_rate() {
    local dev=$1 leaf=$2 rate=$3 burst
    [[ $dev =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]{0,14}$ ]] || return 1
    [[ $leaf =~ ^1[0-9a-f]{2}$ && $leaf != 1ff ]] || return 1
    [[ $rate =~ ^[1-9][0-9]{0,7}$ ]] || return 1
    ((rate <= 34359738)) || return 1
    burst=$(( (rate * 5 + 7) / 8 ))
    ((burst < 2048)) && burst=2048
    tc class change dev "$dev" parent 1: classid "1:$leaf" nsshtb \
        rate "${rate}kbit" burst "$burst" crate "${rate}kbit" cburst "$burst"
}

set_shaper_rate() {
    local direction=$1 device
    [[ $direction == dl || $direction == ul ]] || return 1
    ((shaper_rate_kbps[$direction] != last_shaper_rate_kbps[$direction])) || return 0

    if ((adjust_shaper_rate[$direction])); then
        if [[ $direction == dl ]]; then
            device=$md_nss_dl_device
        else
            device=$md_nss_ul_device
        fi
        md_nss_set_rate "$device" "$md_nss_leaf" "${shaper_rate_kbps[$direction]}" || {
            log_msg "ERROR" "NSS class rate update failed"
            return 1
        }
    fi
    # Same packet-size delay compensation used by the existing NSS adapter.
    ((
        dl_compensation_us=(1000*dl_max_wire_packet_size_bits)/shaper_rate_kbps[dl],
        ul_compensation_us=(1000*ul_max_wire_packet_size_bits)/shaper_rate_kbps[ul],
        compensated_owd_delta_thr_us[dl]=dl_owd_delta_thr_us + dl_compensation_us,
        compensated_owd_delta_thr_us[ul]=ul_owd_delta_thr_us + ul_compensation_us,
        compensated_avg_owd_delta_thr_us[dl]=dl_avg_owd_delta_thr_us + dl_compensation_us,
        compensated_avg_owd_delta_thr_us[ul]=ul_avg_owd_delta_thr_us + ul_compensation_us,
        max_wire_packet_rtt_us=(1000*dl_max_wire_packet_size_bits)/shaper_rate_kbps[dl] +
                             (1000*ul_max_wire_packet_size_bits)/shaper_rate_kbps[ul]
    ))
    last_shaper_rate_kbps[$direction]=${shaper_rate_kbps[$direction]}
}
