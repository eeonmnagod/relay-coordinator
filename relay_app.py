import math
import csv
from datetime import datetime
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# 1. Standard IDMT Curve Constants
CURVE_CONSTANTS = {
    "1.3 Sec curve": 0.0607,
    "3.0 Sec curve": 0.14
}

def parse_voltage_kv(v_str):
    """Extracts numerical kV value from string (e.g., '33 kV' -> 33.0)."""
    try:
        return float(str(v_str).lower().replace("kv", "").strip())
    except ValueError:
        return 11.0

def calc_idmt_time(fault_current, pickup_current, tms, curve_constant):
    """Calculates IDMT relay operating time in seconds."""
    if not pickup_current or pickup_current <= 0 or not tms or tms <= 0:
        return None
    psm = fault_current / pickup_current
    if psm <= 1.0:
        return None
    return (curve_constant / ((psm ** 0.02) - 1.0)) * tms

def calculate_ptr_ngr_limits(v_ll_kv, ngr_ohms, ct_primary, sensitivity_factor=0.20):
    """
    Calculates restricted Earth Fault parameters based on PTR Neutral Earth Resistance (NGR).
    Returns V_LN, Max EF Current, Recommended EF Pickup (Primary A), and Plug Setting (%).
    """
    v_ln_volts = (v_ll_kv * 1000.0) / math.sqrt(3)
    
    if ngr_ohms and ngr_ohms > 0:
        max_ef_current = v_ln_volts / ngr_ohms
        rec_ef_pickup = max_ef_current * sensitivity_factor
    else:
        # Solidly grounded neutral (no intentional NGR limit)
        max_ef_current = None
        rec_ef_pickup = ct_primary * sensitivity_factor

    plug_setting_pct = (rec_ef_pickup / ct_primary) * 100.0 if ct_primary > 0 else 0.0

    return {
        "v_ln_volts": round(v_ln_volts, 2),
        "max_ef_current_A": round(max_ef_current, 2) if max_ef_current else None,
        "recommended_ef_pickup_A": round(rec_ef_pickup, 2),
        "recommended_plug_pct": round(plug_setting_pct, 2)
    }

def calculate_high_set_limit(
    up_pickup, up_tms, up_curve_const,
    down_pickup, down_tms, down_curve_const,
    margin=0.1, max_fault_ceiling=25000.0
):
    """
    Uses bisection search to find the minimum fault current (High Set threshold)
    where (Upstream Time - Downstream Time) drops to the 100ms (0.1s) grading margin.
    """
    low = max(up_pickup, down_pickup) * 1.01
    high = max_fault_ceiling if max_fault_ceiling else 25000.0

    if low >= high:
        return None

    mid = low
    for _ in range(100):
        mid = (low + high) / 2.0
        t_up = calc_idmt_time(mid, up_pickup, up_tms, up_curve_const)
        t_down = calc_idmt_time(mid, down_pickup, down_tms, down_curve_const)

        if t_up is None or t_down is None:
            low = mid + 5.0
            continue

        diff = t_up - t_down
        if abs(diff - margin) < 0.001:
            return round(mid, 2)
        elif diff > margin:
            low = mid
        else:
            high = mid

    return round(mid, 2)

def generate_tcc_plot(up_relay, down_relay, ngr_info, oc_high_set, ef_high_set):
    """Generates interactive side-by-side Log-Log TCC plots for OC and EF coordination."""
    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=("Overcurrent (OC) Coordination", "Earth Fault (EF) Coordination (with PTR NGR)")
    )

    fault_currents = [i for i in range(10, 25000, 25)]
    max_ef = ngr_info["max_ef_current_A"]

    # 1. Overcurrent Curves
    up_oc_c = CURVE_CONSTANTS.get(up_relay["curve"], 0.14)
    down_oc_c = CURVE_CONSTANTS.get(down_relay["curve"], 0.14)

    oc_x_up, oc_y_up, oc_x_down, oc_y_down = [], [], [], []
    for fc in fault_currents:
        t_u = calc_idmt_time(fc, up_relay["oc_pickup"], up_relay["oc_tms"], up_oc_c)
        t_d = calc_idmt_time(fc, down_relay["oc_pickup"], down_relay["oc_tms"], down_oc_c)
        if t_u:
            oc_x_up.append(fc)
            oc_y_up.append(t_u)
        if t_d:
            oc_x_down.append(fc)
            oc_y_down.append(t_d)

    fig.add_trace(go.Scatter(x=oc_x_up, y=oc_y_up, name=f"Upstream OC ({up_relay['feeder']})", line=dict(color="#0288d1", width=2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=oc_x_down, y=oc_y_down, name=f"Downstream OC ({down_relay['feeder']})", line=dict(color="#d32f2f", width=2)), row=1, col=1)

    if oc_high_set:
        fig.add_vline(x=oc_high_set, line_dash="dash", line_color="green", annotation_text=f"OC High Set: {oc_high_set}A", row=1, col=1)

    # 2. Earth Fault Curves (Truncated at Max Restricted EF Current if NGR > 0)
    ef_x_up, ef_y_up, ef_x_down, ef_y_down = [], [], [], []
    for fc in fault_currents:
        if max_ef and fc > max_ef:
            break
        t_u = calc_idmt_time(fc, up_relay["ef_pickup"], up_relay["ef_tms"], up_oc_c)
        t_d = calc_idmt_time(fc, down_relay["ef_pickup"], down_relay["ef_tms"], down_oc_c)
        if t_u:
            ef_x_up.append(fc)
            ef_y_up.append(t_u)
        if t_d:
            ef_x_down.append(fc)
            ef_y_down.append(t_d)

    fig.add_trace(go.Scatter(x=ef_x_up, y=ef_y_up, name=f"Upstream EF ({up_relay['feeder']})", line=dict(color="#0288d1", width=2, dash="dot")), row=1, col=2)
    fig.add_trace(go.Scatter(x=ef_x_down, y=ef_y_down, name=f"Downstream EF ({down_relay['feeder']})", line=dict(color="#d32f2f", width=2, dash="dot")), row=1, col=2)

    if max_ef:
        fig.add_vline(x=max_ef, line_dash="solid", line_color="purple", annotation_text=f"NGR Max EF: {max_ef}A", row=1, col=2)
    if ef_high_set:
        fig.add_vline(x=ef_high_set, line_dash="dash", line_color="green", annotation_text=f"EF High Set: {ef_high_set}A", row=1, col=2)

    fig.update_xaxes(type="log", title_text="Fault Current (A)")
    fig.update_yaxes(type="log", title_text="Operating Time (s)")
    fig.update_layout(title_text=f"STM Relay Coordination - {up_relay['substation']}", height=600)
    fig.show()

if __name__ == "__main__":
    # Example Configuration: 33/11kV Substation with PTR Neutral Earth Resistance
    upstream_ptr_lv = {
        "substation": "CHHINDWARA_33_11KV",
        "feeder": "11kV Main PTR Protection",
        "voltage": "11 kV",
        "ct_primary": 400.0,
        "ngr_ohms": 40.0,  # PTR Neutral Earth Resistance in Ohms (0 if solidly grounded)
        "curve": "3.0 Sec curve",
        "oc_pickup": 300.0,
        "oc_tms": 0.15,
        "ef_pickup": 30.0,
        "ef_tms": 0.12
    }

    downstream_feeder = {
        "substation": "CHHINDWARA_33_11KV",
        "feeder": "11kV Feeder Protection",
        "voltage": "11 kV",
        "ct_primary": 200.0,
        "curve": "3.0 Sec curve",
        "oc_pickup": 150.0,
        "oc_tms": 0.08,
        "ef_pickup": 20.0,
        "ef_tms": 0.05
    }

    v_kv = parse_voltage_kv(upstream_ptr_lv["voltage"])
    ngr_results = calculate_ptr_ngr_limits(v_kv, upstream_ptr_lv["ngr_ohms"], upstream_ptr_lv["ct_primary"])

    oc_hs = calculate_high_set_limit(
        upstream_ptr_lv["oc_pickup"], upstream_ptr_lv["oc_tms"], CURVE_CONSTANTS[upstream_ptr_lv["curve"]],
        downstream_feeder["oc_pickup"], downstream_feeder["oc_tms"], CURVE_CONSTANTS[downstream_feeder["curve"]],
        margin=0.1
    )

    ef_hs = calculate_high_set_limit(
        upstream_ptr_lv["ef_pickup"], upstream_ptr_lv["ef_tms"], CURVE_CONSTANTS[upstream_ptr_lv["curve"]],
        downstream_feeder["ef_pickup"], downstream_feeder["ef_tms"], CURVE_CONSTANTS[downstream_feeder["curve"]],
        margin=0.1,
        max_fault_ceiling=ngr_results["max_ef_current_A"]
    )

    print("=== PTR NGR & RELAY COORDINATION SUMMARY ===")
    print(f"Phase-to-Neutral Voltage : {ngr_results['v_ln_volts']} V")
    print(f"Max Restricted EF Current: {ngr_results['max_ef_current_A']} A")
    print(f"Recommended EF Pickup    : {ngr_results['recommended_ef_pickup_A']} A ({ngr_results['recommended_plug_pct']}% of CT)")
    print(f"100ms OC High-Set Limit  : {oc_hs} A")
    print(f"100ms EF High-Set Limit  : {ef_hs} A")

    generate_tcc_plot(upstream_ptr_lv, downstream_feeder, ngr_results, oc_hs, ef_hs)
