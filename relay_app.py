import math
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

# ---------------------------------------------------------
# 1. CONSTANTS, CONDUCTOR PRESETS & CORE MATH ENGINE
# ---------------------------------------------------------
CURVE_CONSTANTS = {
    "3.0 Sec curve (IEC Standard Inverse)": 0.14,
    "1.3 Sec curve (Fast Inverse)": 0.0607,
}

# Standard 11kV Overhead Conductors & Underground XLPE Cable (Positive Sequence Ohms/km at 50Hz)
CONDUCTOR_PRESETS = {
    "ACSR Dog (100 sq.mm)": {"R1": 0.273, "X1": 0.335},
    "ACSR Raccoon (80 sq.mm)": {"R1": 0.366, "X1": 0.345},
    "ACSR Rabbit (50 sq.mm)": {"R1": 0.543, "X1": 0.359},
    "ACSR Weasel (30 sq.mm)": {"R1": 0.908, "X1": 0.380},
    "11kV XLPE Cable (3x300 sq.mm Al)": {"R1": 0.125, "X1": 0.088},
    "Custom R + jX": {"R1": 0.366, "X1": 0.345},
}

def calc_idmt_time(fault_current, pickup_current, tms, curve_constant):
    """Calculates pure IDMT operating time in seconds for ANSI 51 / 51N / 51G."""
    if not pickup_current or pickup_current <= 0 or not tms or tms <= 0:
        return None
    psm = fault_current / pickup_current
    if psm <= 1.0:
        return None
    return (curve_constant / ((psm ** 0.02) - 1.0)) * tms

def calc_composite_time(
    fault_current, idmt_pickup, tms, curve_constant,
    hs_enabled=False, hs_pickup=None, hs_delay_sec=0.05
):
    """Calculates combined ANSI 51 (IDMT) + ANSI 50 (Instantaneous/Definite-Time High-Set) time."""
    t_idmt = calc_idmt_time(fault_current, idmt_pickup, tms, curve_constant)
    if hs_enabled and hs_pickup and hs_pickup > 0 and fault_current >= hs_pickup:
        if t_idmt is not None:
            return min(t_idmt, hs_delay_sec)
        return hs_delay_sec
    return t_idmt

def calculate_ptr_parameters(v_hv_kv, v_lv_kv, ptr_mva, ptr_z_pct, ngr_ohms, ct_lv_pri, ct_hv_pri):
    """
    Calculates 33/11kV Dyn11 PTR Full Load Currents, Ohmic Source Impedance (X_ptr),
    3-Phase Through-Fault Ceilings (%Z), and Restricted Earth Fault Current (NGR).
    """
    turn_ratio = v_hv_kv / v_lv_kv
    v_lv_ln_volts = (v_lv_kv * 1000.0) / math.sqrt(3)

    i_fl_lv = (ptr_mva * 1e6) / (math.sqrt(3) * v_lv_kv * 1000.0)
    i_fl_hv = (ptr_mva * 1e6) / (math.sqrt(3) * v_hv_kv * 1000.0)

    z_pu = ptr_z_pct / 100.0
    i_sc_3ph_lv = i_fl_lv / z_pu
    i_sc_3ph_hv = i_fl_hv / z_pu

    # PTR Source Impedance referred to 11kV side in Ohms (Z_ptr = (kV^2 / MVA) * z_pu)
    # Using typical X/R = 10 for 8 MVA Power Transformer
    z_ptr_ohms = ((v_lv_kv ** 2) / ptr_mva) * z_pu
    r_ptr_ohms = z_ptr_ohms / math.sqrt(1 + 10**2)
    x_ptr_ohms = r_ptr_ohms * 10.0

    dyn11_lg_hv_factor = 1.0 / (math.sqrt(3) * turn_ratio)

    if ngr_ohms and ngr_ohms > 0:
        max_ef_lv = v_lv_ln_volts / ngr_ohms
        rec_51g_pickup = max_ef_lv * 0.20
    else:
        max_ef_lv = i_sc_3ph_lv
        rec_51g_pickup = ct_lv_pri * 0.20

    max_ef_reflected_hv_line = max_ef_lv * dyn11_lg_hv_factor

    return {
        "turn_ratio": round(turn_ratio, 3),
        "v_lv_ln_volts": round(v_lv_ln_volts, 1),
        "r_ptr_ohms": r_ptr_ohms,
        "x_ptr_ohms": x_ptr_ohms,
        "z_ptr_ohms": round(z_ptr_ohms, 4),
        "i_fl_lv_A": round(i_fl_lv, 2),
        "i_fl_hv_A": round(i_fl_hv, 2),
        "max_3ph_sc_lv_A": round(i_sc_3ph_lv, 1),
        "max_3ph_sc_hv_A": round(i_sc_3ph_hv, 1),
        "max_ef_lv_A": round(max_ef_lv, 2),
        "max_ef_reflected_hv_A": round(max_ef_reflected_hv_line, 2),
        "rec_51g_pickup_A": round(rec_51g_pickup, 2),
        "rec_51g_plug_pct": round((rec_51g_pickup / ct_lv_pri) * 100.0, 2),
        "dyn11_lg_hv_factor": dyn11_lg_hv_factor
    }

def calc_fault_at_distance_km(dist_km, v_ln_volts, r_ptr, x_ptr, r1_km, x1_km):
    """Calculates 3-Phase symmetrical fault current (A) at distance L (km) along the 11kV feeder."""
    r_total = r_ptr + (r1_km * dist_km)
    x_total = x_ptr + (x1_km * dist_km)
    z_total = math.hypot(r_total, x_total)
    return v_ln_volts / z_total if z_total > 0 else 0.0

def calc_ansi_50_reach_km(hs_pickup_amps, v_ln_volts, r_ptr, x_ptr, r1_km, x1_km, total_length_km):
    """
    Solves quadratic impedance equation |(R_ptr + R1*L) + j(X_ptr + X1*L)| = V_LN / I_50
    to find the exact distance L (km) where fault current drops below the ANSI 50 pickup.
    """
    if not hs_pickup_amps or hs_pickup_amps <= 0:
        return 0.0, 0.0

    z_target = v_ln_volts / hs_pickup_amps
    z_ptr = math.hypot(r_ptr, x_ptr)

    # If pickup is set higher than bolted bus fault at 0 km, reach is 0 km
    if z_target <= z_ptr:
        return 0.0, 0.0

    # Quadratic coefficients: a*L^2 + b*L + c = 0
    a = (r1_km ** 2) + (x1_km ** 2)
    b = 2.0 * ((r_ptr * r1_km) + (x_ptr * x1_km))
    c = (r_ptr ** 2) + (x_ptr ** 2) - (z_target ** 2)

    if a <= 0:
        return 0.0, 0.0

    discriminant = (b ** 2) - (4.0 * a * c)
    if discriminant < 0:
        return 0.0, 0.0

    reach_km = (-b + math.sqrt(discriminant)) / (2.0 * a)
    reach_pct = (reach_km / total_length_km) * 100.0 if total_length_km > 0 else 0.0
    return round(max(0.0, reach_km), 2), round(max(0.0, reach_pct), 1)

def calculate_high_set_bisection(
    up_pickup, up_tms, up_c,
    down_pickup, down_tms, down_c,
    cti_sec=0.10, max_ceiling=25000.0
):
    """Finds minimum fault current where pure IDMT (Upstream - Downstream) drops below CTI."""
    low = max(up_pickup, down_pickup) * 1.01
    high = float(max_ceiling)
    if low >= high:
        return None

    t_up_max = calc_idmt_time(high, up_pickup, up_tms, up_c)
    t_down_max = calc_idmt_time(high, down_pickup, down_tms, down_c)
    if t_up_max and t_down_max and (t_up_max - t_down_max) >= cti_sec:
        return None

    mid = low
    for _ in range(100):
        mid = (low + high) / 2.0
        t_up = calc_idmt_time(mid, up_pickup, up_tms, up_c)
        t_down = calc_idmt_time(mid, down_pickup, down_tms, down_c)
        if t_up is None or t_down is None:
            low = mid + 2.0
            continue
        diff = t_up - t_down
        if abs(diff - cti_sec) < 0.001:
            return round(mid, 1)
        elif diff > cti_sec:
            low = mid
        else:
            high = mid
    return round(mid, 1)

# ---------------------------------------------------------
# 2. STREAMLIT WEB APP UI
# ---------------------------------------------------------
st.set_page_config(page_title="STM 3-Zone Relay & Distance Reach", layout="wide")
st.title("⚡ 3-Zone Relay Coordination & 11kV Feeder Distance-Reach Web App")
st.markdown(
    "Simulates **3-Zone Composite IDMT (51) + High-Set (50) Coordination** across a **33/11kV Dyn11 PTR** "
    "and calculates the **exact kilometer reach ($L_{\\text{reach}}$)** of the **ANSI 50** element using **Line Impedance ($R_1 + jX_1$ / km)**."
)

# Sidebar: PTR, Breaker & Feeder Line Impedance Specs
with st.sidebar:
    st.header("1. PTR & Substation Specs")
    substation_name = st.text_input("Substation Name", value="CHHINDWARA_33_11KV")
    ptr_mva = st.number_input("PTR Rating (MVA)", min_value=1.0, max_value=100.0, value=8.0, step=0.5)
    ptr_z_pct = st.number_input("PTR Impedance (%Z)", min_value=1.0, max_value=25.0, value=8.35, step=0.05)
    ngr_ohms = st.number_input("PTR Neutral Earth Resistance (NGR Ω) [0 = Solid]", min_value=0.0, max_value=500.0, value=40.0, step=1.0)

    st.header("2. 11kV Feeder Line Impedance")
    conductor_choice = st.selectbox("Conductor / Cable Type", list(CONDUCTOR_PRESETS.keys()), index=0)
    default_r1 = CONDUCTOR_PRESETS[conductor_choice]["R1"]
    default_x1 = CONDUCTOR_PRESETS[conductor_choice]["X1"]
    is_custom = conductor_choice == "Custom R + jX"

    r1_per_km = st.number_input("Positive Seq Resistance R1 (Ω/km)", min_value=0.01, max_value=5.0, value=default_r1, step=0.01, format="%.3f", disabled=not is_custom)
    x1_per_km = st.number_input("Positive Seq Reactance X1 (Ω/km)", min_value=0.01, max_value=5.0, value=default_x1, step=0.01, format="%.3f", disabled=not is_custom)
    feeder_length_km = st.number_input("Total 11kV Feeder Length (km)", min_value=0.5, max_value=100.0, value=12.0, step=0.5)
    target_reach_pct = st.slider("Target ANSI 50 Protected Reach (%)", min_value=50, max_value=90, value=80, step=5)

    st.header("3. Breaker (ANSI 52) Grading")
    cb_open_ms = st.number_input("VCB (52) Interrupting Time (ms)", min_value=20.0, max_value=200.0, value=60.0, step=5.0)
    overshoot_ms = st.number_input("Relay Overshoot & Safety Margin (ms)", min_value=10.0, max_value=200.0, value=40.0, step=5.0)
    total_cti_sec = (cb_open_ms + overshoot_ms) / 1000.0
    st.info(f"**Required Grading Margin (CTI):** `{total_cti_sec*1000:.0f} ms ({total_cti_sec:.2f} s)`")

# Pre-calculate PTR and Feeder Distance Faults so we can show Recommended 80% Pickup in Zone 1 UI
ptr_data = calculate_ptr_parameters(33.0, 11.0, ptr_mva, ptr_z_pct, ngr_ohms, 400.0, 200.0)
target_reach_km = feeder_length_km * (target_reach_pct / 100.0)
fault_at_target_reach = calc_fault_at_distance_km(
    target_reach_km, ptr_data["v_lv_ln_volts"], ptr_data["r_ptr_ohms"], ptr_data["x_ptr_ohms"], r1_per_km, x1_per_km
)
fault_at_feeder_end = calc_fault_at_distance_km(
    feeder_length_km, ptr_data["v_lv_ln_volts"], ptr_data["r_ptr_ohms"], ptr_data["x_ptr_ohms"], r1_per_km, x1_per_km
)

# Main Relay Settings Columns (3 Protection Zones)
col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("Zone 1: 11kV Outgoing Feeder")
    st.caption(f"Rec. {target_reach_pct}% Zone-1 50 Pickup: **{fault_at_target_reach:.0f} A** ({target_reach_km:.1f} km)")
    fdr_ct = st.number_input("Feeder CT Primary (A)", value=200.0, step=50.0)
    fdr_curve_name = st.selectbox("Feeder IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0, key="fdr_c")
    
    st.markdown("**ANSI 51 / 51N (IDMT Settings)**")
    fdr_oc_pickup = st.number_input("Feeder 51 OC Pickup (A)", value=150.0, step=10.0)
    fdr_oc_tms = st.number_input("Feeder 51 OC TMS", value=0.08, step=0.01, format="%.2f")
    fdr_ef_pickup = st.number_input("Feeder 51N EF Pickup (A)", value=20.0, step=5.0)
    fdr_ef_tms = st.number_input("Feeder 51N EF TMS", value=0.05, step=0.01, format="%.2f")

    st.markdown("**ANSI 50 / 50N (High-Set Settings)**")
    fdr_50_en = st.checkbox("Enable Feeder ANSI 50 (Phase High-Set)", value=True)
    fdr_50_pickup = st.number_input("Feeder 50 High-Set Pickup (A)", value=float(round(fault_at_target_reach, -1)), step=50.0, disabled=not fdr_50_en)
    fdr_50_delay = st.number_input("Feeder 50 Operating Time (s)", min_value=0.02, max_value=2.0, value=0.05, step=0.01, format="%.2f", disabled=not fdr_50_en)

    fdr_50n_en = st.checkbox("Enable Feeder ANSI 50N (EF High-Set)", value=True)
    fdr_50n_pickup = st.number_input("Feeder 50N High-Set Pickup (A)", value=80.0, step=5.0, disabled=not fdr_50n_en)
    fdr_50n_delay = st.number_input("Feeder 50N Operating Time (s)", min_value=0.02, max_value=2.0, value=0.05, step=0.01, format="%.2f", disabled=not fdr_50n_en)

with col2:
    st.subheader("Zone 2: 11kV PTR LV Incomer")
    st.caption("Local Backup (Star Winding + NGR)")
    lv_ct = st.number_input("PTR LV CT Primary (A)", value=400.0, step=50.0)
    lv_curve_name = st.selectbox("PTR LV IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0, key="lv_c")
    
    st.markdown("**ANSI 51 / 51G (IDMT Settings)**")
    lv_oc_pickup = st.number_input("PTR LV 51 OC Pickup (A)", value=300.0, step=10.0)
    lv_oc_tms = st.number_input("PTR LV 51 OC TMS", value=0.15, step=0.01, format="%.2f")
    lv_ef_pickup = st.number_input("PTR Neutral 51G Pickup (A)", value=30.0, step=5.0)
    lv_ef_tms = st.number_input("PTR Neutral 51G TMS", value=0.12, step=0.01, format="%.2f")

    st.markdown("**ANSI 50 / 50G (High-Set Settings)**")
    lv_50_en = st.checkbox("Enable PTR LV ANSI 50 (Phase High-Set)", value=False)
    lv_50_pickup = st.number_input("PTR LV 50 High-Set Pickup (A)", value=2800.0, step=100.0, disabled=not lv_50_en)
    lv_50_delay = st.number_input("PTR LV 50 Time Delay (s)", min_value=0.02, max_value=2.0, value=0.20, step=0.01, format="%.2f", disabled=not lv_50_en)

    lv_50g_en = st.checkbox("Enable PTR Neutral ANSI 50G (EF High-Set)", value=False)
    lv_50g_pickup = st.number_input("PTR Neutral 50G Pickup (A)", value=130.0, step=5.0, disabled=not lv_50g_en)
    lv_50g_delay = st.number_input("PTR Neutral 50G Time Delay (s)", min_value=0.02, max_value=2.0, value=0.20, step=0.01, format="%.2f", disabled=not lv_50g_en)

with col3:
    st.subheader("Zone 3: 33kV PTR HV Primary")
    st.caption("Remote/HV Backup (Delta Winding)")
    hv_ct = st.number_input("PTR HV CT Primary (A at 33kV)", value=200.0, step=50.0)
    hv_curve_name = st.selectbox("PTR HV IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0, key="hv_c")
    
    st.markdown("**ANSI 51 (IDMT Settings)**")
    hv_oc_pickup_33kv = st.number_input("PTR HV 51 OC Pickup (A at 33kV)", value=125.0, step=5.0)
    hv_oc_tms = st.number_input("PTR HV 51 OC TMS", value=0.22, step=0.01, format="%.2f")

    st.markdown("**ANSI 50 (High-Set Settings at 33kV)**")
    hv_50_en = st.checkbox("Enable PTR HV ANSI 50 (33kV High-Set)", value=True)
    hv_50_pickup_33kv = st.number_input("PTR HV 50 High-Set Pickup (A at 33kV)", value=1850.0, step=50.0, disabled=not hv_50_en)
    hv_50_delay = st.number_input("PTR HV 50 Operating Time (s)", min_value=0.02, max_value=2.0, value=0.05, step=0.01, format="%.2f", disabled=not hv_50_en)

# ---------------------------------------------------------
# 3. RUN CALCULATIONS, REACH & HIGH-SET BISECTION
# ---------------------------------------------------------
ptr_data = calculate_ptr_parameters(33.0, 11.0, ptr_mva, ptr_z_pct, ngr_ohms, lv_ct, hv_ct)

# Calculate Actual Reach of User's Feeder ANSI 50 Setting
actual_reach_km, actual_reach_pct = calc_ansi_50_reach_km(
    fdr_50_pickup if fdr_50_en else 0.0,
    ptr_data["v_lv_ln_volts"], ptr_data["r_ptr_ohms"], ptr_data["x_ptr_ohms"],
    r1_per_km, x1_per_km, feeder_length_km
)

c_fdr = CURVE_CONSTANTS[fdr_curve_name]
c_lv = CURVE_CONSTANTS[lv_curve_name]
c_hv = CURVE_CONSTANTS[hv_curve_name]

hv_oc_pickup_referred_11kv_3ph = hv_oc_pickup_33kv * ptr_data["turn_ratio"]
hv_50_pickup_referred_11kv_3ph = hv_50_pickup_33kv * ptr_data["turn_ratio"]

hv_oc_pickup_referred_11kv_lg = hv_oc_pickup_33kv * ptr_data["turn_ratio"] * math.sqrt(3)
hv_50_pickup_referred_11kv_lg = hv_50_pickup_33kv * ptr_data["turn_ratio"] * math.sqrt(3)

rec_50_fdr_vs_lv = calculate_high_set_bisection(
    lv_oc_pickup, lv_oc_tms, c_lv,
    fdr_oc_pickup, fdr_oc_tms, c_fdr,
    cti_sec=total_cti_sec, max_ceiling=ptr_data["max_3ph_sc_lv_A"]
)

rec_50n_fdr_vs_lv = calculate_high_set_bisection(
    lv_ef_pickup, lv_ef_tms, c_lv,
    fdr_ef_pickup, fdr_ef_tms, c_fdr,
    cti_sec=total_cti_sec, max_ceiling=ptr_data["max_ef_lv_A"]
)

# ---------------------------------------------------------
# 4. KPI METRICS SUMMARY
# ---------------------------------------------------------
st.divider()
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("11kV Bus Fault (0 km)", f"{ptr_data['max_3ph_sc_lv_A']} A", f"PTR Z: {ptr_data['z_ptr_ohms']} Ω")
m2.metric(f"Fault at {target_reach_pct}% Line ({target_reach_km:.1f} km)", f"{fault_at_target_reach:.0f} A", f"Rec. 50 Setting")
m3.metric(f"Fault at 100% Line ({feeder_length_km:.1f} km)", f"{fault_at_feeder_end:.0f} A", f"Min 3Ph Fault")
m4.metric(
    "Actual ANSI 50 Line Reach",
    f"{actual_reach_km} km ({actual_reach_pct}%)" if fdr_50_en else "Disabled",
    "✅ Safe (<85%)" if actual_reach_pct <= 85 else "⚠️ Overreaching (>85%)"
)
m5.metric(
    "Max NGR Restricted EF (11kV)",
    f"{ptr_data['max_ef_lv_A']} A",
    f"Rec 51G: {ptr_data['rec_51g_pickup_A']} A ({ptr_data['rec_51g_plug_pct']}%)"
)

# ---------------------------------------------------------
# 5. INTERACTIVE TCC CHARTS & DISTANCE-REACH PROFILE
# ---------------------------------------------------------
def build_curve_trace(fc_list, idmt_pickup, tms, c_const, hs_en, hs_pickup, hs_delay):
    pts = list(fc_list)
    if hs_en and hs_pickup and hs_pickup > pts[0] and hs_pickup <= pts[-1]:
        pts.extend([hs_pickup - 0.1, hs_pickup])
        pts = sorted(list(set(pts)))
    x_vals, y_vals = [], []
    for fc in pts:
        t = calc_composite_time(fc, idmt_pickup, tms, c_const, hs_en, hs_pickup, hs_delay)
        if t is not None:
            x_vals.append(fc)
            y_vals.append(t)
    return x_vals, y_vals

fig = make_subplots(
    rows=1, cols=3,
    subplot_titles=(
        "1. Phase Overcurrent (ANSI 50+51) TCC",
        "2. Earth Fault (50N/51N + 51G) TCC",
        "3. 11kV Feeder Fault vs. Distance (ANSI 50 Reach)"
    )
)

max_3ph = int(ptr_data["max_3ph_sc_lv_A"])
max_ef = int(ptr_data["max_ef_lv_A"])

# --- Plot 1: Phase Overcurrent TCC ---
fc_oc_range = list(range(20, int(max_3ph * 1.25) + 25, 25))
fdr_oc_x, fdr_oc_y = build_curve_trace(fc_oc_range, fdr_oc_pickup, fdr_oc_tms, c_fdr, fdr_50_en, fdr_50_pickup, fdr_50_delay)
lv_oc_x, lv_oc_y = build_curve_trace(fc_oc_range, lv_oc_pickup, lv_oc_tms, c_lv, lv_50_en, lv_50_pickup, lv_50_delay)
hv_oc_x, hv_oc_y = build_curve_trace(fc_oc_range, hv_oc_pickup_referred_11kv_3ph, hv_oc_tms, c_hv, hv_50_en, hv_50_pickup_referred_11kv_3ph, hv_50_delay)

fig.add_trace(go.Scatter(x=fdr_oc_x, y=fdr_oc_y, name="Zone 1: Feeder (51+50)", line=dict(color="#d32f2f", width=2.5)), row=1, col=1)
fig.add_trace(go.Scatter(x=lv_oc_x, y=lv_oc_y, name="Zone 2: PTR LV (51+50)", line=dict(color="#0288d1", width=2.5)), row=1, col=1)
fig.add_trace(go.Scatter(x=hv_oc_x, y=hv_oc_y, name="Zone 3: PTR HV (Ref 11kV)", line=dict(color="#388e3c", width=2.5, dash="dash")), row=1, col=1)
fig.add_vline(x=max_3ph, line_dash="solid", line_color="black", annotation_text=f"0km Bus: {max_3ph}A", row=1, col=1)

# --- Plot 2: Earth Fault TCC ---
step_ef = max(2, int(max_ef / 120))
fc_ef_range = list(range(5, max_ef + step_ef, step_ef))
fdr_ef_x, fdr_ef_y = build_curve_trace(fc_ef_range, fdr_ef_pickup, fdr_ef_tms, c_fdr, fdr_50n_en, fdr_50n_pickup, fdr_50n_delay)
lv_ef_x, lv_ef_y = build_curve_trace(fc_ef_range, lv_ef_pickup, lv_ef_tms, c_lv, lv_50g_en, lv_50g_pickup, lv_50g_delay)
hv_lg_x, hv_lg_y = build_curve_trace(fc_ef_range, hv_oc_pickup_referred_11kv_lg, hv_oc_tms, c_hv, hv_50_en, hv_50_pickup_referred_11kv_lg, hv_50_delay)

fig.add_trace(go.Scatter(x=fdr_ef_x, y=fdr_ef_y, name="Zone 1: Feeder (51N+50N)", line=dict(color="#d32f2f", width=2.5, dash="dot")), row=1, col=2)
fig.add_trace(go.Scatter(x=lv_ef_x, y=lv_ef_y, name="Zone 2: PTR Neutral (51G)", line=dict(color="#0288d1", width=2.5, dash="dot")), row=1, col=2)
fig.add_trace(go.Scatter(x=hv_lg_x, y=hv_lg_y, name="Zone 3: 33kV Dyn11 L-G", line=dict(color="#388e3c", width=2, dash="dashdot")), row=1, col=2)
fig.add_vline(x=max_ef, line_dash="solid", line_color="purple", annotation_text=f"NGR Max: {max_ef}A", row=1, col=2)

# --- Plot 3: 11kV Feeder Fault Current vs. Distance (km) ---
dist_points = [round(i * (feeder_length_km / 100.0), 2) for i in range(0, 101)]
fault_profile_y = [
    calc_fault_at_distance_km(d, ptr_data["v_lv_ln_volts"], ptr_data["r_ptr_ohms"], ptr_data["x_ptr_ohms"], r1_per_km, x1_per_km)
    for d in dist_points
]

fig.add_trace(
    go.Scatter(x=dist_points, y=fault_profile_y, name="3Ph Fault Current vs km", line=dict(color="#6a1b9a", width=3)),
    row=1, col=3
)
if fdr_50_en:
    fig.add_hline(y=fdr_50_pickup, line_dash="dash", line_color="#d32f2f", annotation_text=f"ANSI 50 Pickup: {fdr_50_pickup:.0f}A", row=1, col=3)
    fig.add_vline(x=actual_reach_km, line_dash="dot", line_color="green", annotation_text=f"Reach: {actual_reach_km}km ({actual_reach_pct}%)", row=1, col=3)

fig.update_xaxes(type="log", title_text="Fault Current (A)", row=1, col=1)
fig.update_yaxes(type="log", title_text="Operating Time (s)", range=[-1.6, 2.0], row=1, col=1)

fig.update_xaxes(type="log", title_text="Fault Current (A)", row=1, col=2)
fig.update_yaxes(type="log", title_text="Operating Time (s)", range=[-1.6, 2.0], row=1, col=2)

fig.update_xaxes(title_text="Feeder Distance from Substation (km)", row=1, col=3)
fig.update_yaxes(title_text="3-Phase Fault Current (A)", row=1, col=3)

fig.update_layout(height=550, legend=dict(orientation="h", yanchor="bottom", y=-0.28, xanchor="center", x=0.5))
st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------
# 6. DISTANCE-BASED & COMPOSITE SELECTIVITY AUDIT TABLE
# ---------------------------------------------------------
st.subheader("📋 Feeder Distance & 3-Zone Composite (ANSI 51 + 50) Selectivity Audit Table")

distance_checkpoints_pct = [0, 25, 50, target_reach_pct, 85, 100]
distance_checkpoints_pct = sorted(list(set(distance_checkpoints_pct)))

audit_rows = []
for pct in distance_checkpoints_pct:
    d_km = round(feeder_length_km * (pct / 100.0), 2)
    fc = round(calc_fault_at_distance_km(
        d_km, ptr_data["v_lv_ln_volts"], ptr_data["r_ptr_ohms"], ptr_data["x_ptr_ohms"], r1_per_km, x1_per_km
    ), 1)

    t1_oc = calc_composite_time(fc, fdr_oc_pickup, fdr_oc_tms, c_fdr, fdr_50_en, fdr_50_pickup, fdr_50_delay)
    t2_oc = calc_composite_time(fc, lv_oc_pickup, lv_oc_tms, c_lv, lv_50_en, lv_50_pickup, lv_50_delay)
    t3_oc = calc_composite_time(fc, hv_oc_pickup_referred_11kv_3ph, hv_oc_tms, c_hv, hv_50_en, hv_50_pickup_referred_11kv_3ph, hv_50_delay)

    z1_element = "50 (High-Set)" if (fdr_50_en and fc >= fdr_50_pickup) else ("51 (IDMT)" if t1_oc else "None")
    cti_12_ms = (t2_oc - t1_oc) * 1000.0 if (t1_oc and t2_oc) else None
    cti_23_ms = (t3_oc - t2_oc) * 1000.0 if (t2_oc and t3_oc) else None

    status = "✅ PASS (Selective)"
    if (cti_12_ms is not None and cti_12_ms < total_cti_sec * 1000.0) or (cti_23_ms is not None and cti_23_ms < total_cti_sec * 1000.0):
        status = "⚠️ Grading Margin Violated (< CTI)"

    audit_rows.append({
        "Feeder Location": f"{pct}% ({d_km} km)",
        "11kV 3Ph Fault (A)": fc,
        "Reflected 33kV 3Ph (A)": round(fc / ptr_data["turn_ratio"], 1),
        "Active Z1 Element": z1_element,
        "Zone 1 Feeder Time (s)": round(t1_oc, 3) if t1_oc else "No Pickup",
        "Zone 2 PTR LV Time (s)": round(t2_oc, 3) if t2_oc else "No Pickup",
        "Zone 3 PTR HV Time (s)": round(t3_oc, 3) if t3_oc else "No Pickup",
        "Margin Z2-Z1 (ms)": round(cti_12_ms, 1) if cti_12_ms else "N/A",
        "Margin Z3-Z2 (ms)": round(cti_23_ms, 1) if cti_23_ms else "N/A",
        "Audit Verdict": status
    })

df_audit = pd.DataFrame(audit_rows)
st.dataframe(df_audit, use_container_width=True)

csv_bytes = df_audit.to_csv(index=False).encode("utf-8")
st.download_button(
    label=f"📥 Download {substation_name} Distance & Relay Coordination Audit CSV",
    data=csv_bytes,
    file_name=f"{substation_name}_Distance_Reach_Audit.csv",
    mime="text/csv"
)
