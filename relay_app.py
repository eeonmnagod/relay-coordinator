import math
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

# ---------------------------------------------------------
# 1. CONSTANTS & CORE PROTECTION MATH ENGINE
# ---------------------------------------------------------
CURVE_CONSTANTS = {
    "3.0 Sec curve (IEC Standard Inverse)": 0.14,
    "1.3 Sec curve (Fast Inverse)": 0.0607,
}

def calc_idmt_time(fault_current, pickup_current, tms, curve_constant):
    """Calculates IDMT operating time in seconds for ANSI 51 / 51N / 51G."""
    if not pickup_current or pickup_current <= 0 or not tms or tms <= 0:
        return None
    psm = fault_current / pickup_current
    if psm <= 1.0:
        return None
    return (curve_constant / ((psm ** 0.02) - 1.0)) * tms

def calculate_ptr_parameters(v_hv_kv, v_lv_kv, ptr_mva, ptr_z_pct, ngr_ohms, ct_lv_pri, ct_hv_pri):
    """
    Calculates 33/11kV Dyn11 PTR Full Load Currents, 3-Phase Through-Fault Ceilings (%Z),
    and Restricted Earth Fault Current (NGR) referred to both 11kV LV and 33kV HV sides.
    """
    turn_ratio = v_hv_kv / v_lv_kv  # 33 / 11 = 3.0
    v_lv_ln_volts = (v_lv_kv * 1000.0) / math.sqrt(3)

    # Full Load Currents (LV & HV)
    i_fl_lv = (ptr_mva * 1e6) / (math.sqrt(3) * v_lv_kv * 1000.0)
    i_fl_hv = (ptr_mva * 1e6) / (math.sqrt(3) * v_hv_kv * 1000.0)

    # Max 3-Phase Symmetrical Through-Fault Current (%Z Ceiling)
    z_pu = ptr_z_pct / 100.0
    i_sc_3ph_lv = i_fl_lv / z_pu
    i_sc_3ph_hv = i_fl_hv / z_pu

    # Dyn11 Phase-to-Phase Fault Factor on HV line (2 / sqrt(3) or 1/sqrt(3) distribution)
    # For an 11kV L-G fault on a Star winding, Delta winding blocks zero-sequence (33kV 51N = 0A),
    # and reflects into one HV line conductor at I_HV_line = I_LV_fault / (sqrt(3) * Turn_Ratio)
    dyn11_lg_hv_factor = 1.0 / (math.sqrt(3) * turn_ratio)

    # NGR Restricted Earth Fault Current on 11kV Side
    if ngr_ohms and ngr_ohms > 0:
        max_ef_lv = v_lv_ln_volts / ngr_ohms
        rec_51g_pickup = max_ef_lv * 0.20  # 20% sensitivity rule
    else:
        max_ef_lv = i_sc_3ph_lv  # Solidly grounded
        rec_51g_pickup = ct_lv_pri * 0.20

    # Reflected 11kV Earth Fault Current seen by 33kV Phase Overcurrent (51) Relay
    max_ef_reflected_hv_line = max_ef_lv * dyn11_lg_hv_factor

    return {
        "turn_ratio": round(turn_ratio, 3),
        "v_lv_ln_volts": round(v_lv_ln_volts, 1),
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

def calculate_high_set_bisection(
    up_pickup, up_tms, up_c,
    down_pickup, down_tms, down_c,
    cti_sec=0.10, max_ceiling=25000.0
):
    """
    Finds the minimum fault current (ANSI 50 / 50N threshold) where
    (Upstream Time - Downstream Time) drops below the required Grading Margin (CTI).
    """
    low = max(up_pickup, down_pickup) * 1.01
    high = float(max_ceiling)

    if low >= high:
        return None

    t_up_max = calc_idmt_time(high, up_pickup, up_tms, up_c)
    t_down_max = calc_idmt_time(high, down_pickup, down_tms, down_c)
    if t_up_max and t_down_max and (t_up_max - t_down_max) >= cti_sec:
        return None  # Selectivity holds all the way to the physical fault ceiling

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
st.set_page_config(page_title="STM 3-Zone Relay Coordination", layout="wide")
st.title("⚡ 3-Stage Protection Zone Coordination Web App (11kV ➔ PTR LV ➔ 33kV PTR HV)")
st.markdown(
    "Simulates **3-Zone IDMT Coordination (ANSI 50/51/51N/51G)** across a **33/11kV Dyn11 Power Transformer**, "
    "incorporating **PTR %Z Through-Fault Ceilings**, **NGR Restricted Earth Fault Clamping**, and **Delta-Star Vector Group Phase-Shift Compensation**."
)

# Sidebar: PTR & System Parameters
with st.sidebar:
    st.header("1. PTR & Substation Specs")
    substation_name = st.text_input("Substation Name", value="CHHINDWARA_33_11KV")
    ptr_mva = st.number_input("PTR Rating (MVA)", min_value=1.0, max_value=100.0, value=8.0, step=0.5)
    ptr_z_pct = st.number_input("PTR Impedance (%Z)", min_value=1.0, max_value=25.0, value=8.35, step=0.05)
    ngr_ohms = st.number_input("PTR Neutral Earth Resistance (NGR Ω) [0 = Solid]", min_value=0.0, max_value=500.0, value=40.0, step=1.0)
    
    st.header("2. Breaker (ANSI 52) Grading")
    cb_open_ms = st.number_input("VCB (52) Interrupting Time (ms)", min_value=20.0, max_value=200.0, value=60.0, step=5.0)
    overshoot_ms = st.number_input("Relay Overshoot & Safety Margin (ms)", min_value=10.0, max_value=200.0, value=40.0, step=5.0)
    total_cti_sec = (cb_open_ms + overshoot_ms) / 1000.0
    st.info(f"**Required Grading Margin (CTI):** `{total_cti_sec*1000:.0f} ms ({total_cti_sec:.2f} s)`")

# Main Relay Settings Columns (3 Protection Zones)
col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("Zone 1: 11kV Outgoing Feeder")
    st.caption("Primary Protection (Downstream)")
    fdr_ct = st.number_input("Feeder CT Primary (A)", value=200.0, step=50.0)
    fdr_curve_name = st.selectbox("Feeder IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0, key="fdr_c")
    fdr_oc_pickup = st.number_input("Feeder 51 OC Pickup (A)", value=150.0, step=10.0)
    fdr_oc_tms = st.number_input("Feeder 51 OC TMS", value=0.08, step=0.01, format="%.2f")
    fdr_ef_pickup = st.number_input("Feeder 51N EF Pickup (A)", value=20.0, step=5.0)
    fdr_ef_tms = st.number_input("Feeder 51N EF TMS", value=0.05, step=0.01, format="%.2f")

with col2:
    st.subheader("Zone 2: 11kV PTR LV Incomer")
    st.caption("Local Backup (Star Winding + NGR)")
    lv_ct = st.number_input("PTR LV CT Primary (A)", value=400.0, step=50.0)
    lv_curve_name = st.selectbox("PTR LV IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0, key="lv_c")
    lv_oc_pickup = st.number_input("PTR LV 51 OC Pickup (A)", value=300.0, step=10.0)
    lv_oc_tms = st.number_input("PTR LV 51 OC TMS", value=0.15, step=0.01, format="%.2f")
    lv_ef_pickup = st.number_input("PTR Neutral 51G Pickup (A)", value=30.0, step=5.0)
    lv_ef_tms = st.number_input("PTR Neutral 51G TMS", value=0.12, step=0.01, format="%.2f")

with col3:
    st.subheader("Zone 3: 33kV PTR HV Primary")
    st.caption("Remote/HV Backup (Delta Winding)")
    hv_ct = st.number_input("PTR HV CT Primary (A at 33kV)", value=200.0, step=50.0)
    hv_curve_name = st.selectbox("PTR HV IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0, key="hv_c")
    hv_oc_pickup_33kv = st.number_input("PTR HV 51 OC Pickup (A at 33kV)", value=125.0, step=5.0)
    hv_oc_tms = st.number_input("PTR HV 51 OC TMS", value=0.22, step=0.01, format="%.2f")
    st.markdown(
        "ℹ️ **Dyn11 Vector Group Note:** An 11kV Single-Line-to-Ground fault circulates inside the 33kV Delta winding. "
        "**Zero-sequence current cannot exit to the 33kV bus**, so the 33kV `51N` relay sees `0 A`. "
        "Instead, the 33kV Phase `51` relay sees $I_{LV,EF} / (\\sqrt{3} \\times N)$."
    )

# ---------------------------------------------------------
# 3. RUN CALCULATIONS & HIGH-SET BISECTION
# ---------------------------------------------------------
ptr_data = calculate_ptr_parameters(33.0, 11.0, ptr_mva, ptr_z_pct, ngr_ohms, lv_ct, hv_ct)

c_fdr = CURVE_CONSTANTS[fdr_curve_name]
c_lv = CURVE_CONSTANTS[lv_curve_name]
c_hv = CURVE_CONSTANTS[hv_curve_name]

# Refer 33kV HV Phase OC Pickup to 11kV Equivalent Base:
# 1) For 3-Phase Balanced Faults: I_11kV_eq = I_33kV_pickup * Turn_Ratio (3.0)
hv_oc_pickup_referred_11kv_3ph = hv_oc_pickup_33kv * ptr_data["turn_ratio"]

# 2) For 11kV Single-Line-to-Ground Faults across Dyn11 Delta-Star:
# Only 1/sqrt(3) of per-unit fault current appears on one HV phase conductor: I_11kV_eq_LG = I_33kV_pickup * Turn_Ratio * sqrt(3)
hv_oc_pickup_referred_11kv_lg = hv_oc_pickup_33kv * ptr_data["turn_ratio"] * math.sqrt(3)

# High-Set Bisection Thresholds (11kV Base)
hs_50_fdr_vs_lv = calculate_high_set_bisection(
    lv_oc_pickup, lv_oc_tms, c_lv,
    fdr_oc_pickup, fdr_oc_tms, c_fdr,
    cti_sec=total_cti_sec, max_ceiling=ptr_data["max_3ph_sc_lv_A"]
)

hs_50_lv_vs_hv = calculate_high_set_bisection(
    hv_oc_pickup_referred_11kv_3ph, hv_oc_tms, c_hv,
    lv_oc_pickup, lv_oc_tms, c_lv,
    cti_sec=total_cti_sec, max_ceiling=ptr_data["max_3ph_sc_lv_A"]
)

hs_50n_fdr_vs_lv = calculate_high_set_bisection(
    lv_ef_pickup, lv_ef_tms, c_lv,
    fdr_ef_pickup, fdr_ef_tms, c_fdr,
    cti_sec=total_cti_sec, max_ceiling=ptr_data["max_ef_lv_A"]
)

# ---------------------------------------------------------
# 4. KPI METRICS SUMMARY
# ---------------------------------------------------------
st.divider()
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("PTR Full Load (11kV / 33kV)", f"{ptr_data['i_fl_lv_A']} A / {ptr_data['i_fl_hv_A']} A")
m2.metric("Max 3Ph Fault (%Z Ceiling)", f"{ptr_data['max_3ph_sc_lv_A']} A (11kV)", f"{ptr_data['max_3ph_sc_hv_A']} A at 33kV")
m3.metric("Max NGR Restricted EF (11kV)", f"{ptr_data['max_ef_lv_A']} A", f"33kV Line Sees: {ptr_data['max_ef_reflected_hv_A']} A")
m4.metric("Rec. 51G Pickup (20% NGR)", f"{ptr_data['rec_51g_pickup_A']} A", f"Plug: {ptr_data['rec_51g_plug_pct']}% of CT")
m5.metric(
    "ANSI 50 / 50N High-Set Limits",
    f"50: {hs_50_fdr_vs_lv if hs_50_fdr_vs_lv else 'Selective'} A",
    f"50N: {hs_50n_fdr_vs_lv if hs_50n_fdr_vs_lv else 'Selective'} A"
)

# ---------------------------------------------------------
# 5. INTERACTIVE PLOTLY TCC CHARTS (REFERRED TO 11kV BASE)
# ---------------------------------------------------------
fig = make_subplots(
    rows=1, cols=2,
    subplot_titles=(
        "Phase Overcurrent (ANSI 50/51) - 3 Zones (11kV Base)",
        "Earth Fault (51N / 51G / Dyn11 33kV Reflected) - NGR Bounded"
    )
)

max_3ph = int(ptr_data["max_3ph_sc_lv_A"])
max_ef = int(ptr_data["max_ef_lv_A"])

# --- Plot 1: 3-Zone Phase Overcurrent (51) ---
fc_oc_range = list(range(20, max_3ph + 25, 25))
fdr_oc_x, fdr_oc_y, lv_oc_x, lv_oc_y, hv_oc_x, hv_oc_y = [], [], [], [], [], []

for fc in fc_oc_range:
    t_f = calc_idmt_time(fc, fdr_oc_pickup, fdr_oc_tms, c_fdr)
    t_l = calc_idmt_time(fc, lv_oc_pickup, lv_oc_tms, c_lv)
    t_h = calc_idmt_time(fc, hv_oc_pickup_referred_11kv_3ph, hv_oc_tms, c_hv)
    if t_f: fdr_oc_x.append(fc); fdr_oc_y.append(t_f)
    if t_l: lv_oc_x.append(fc); lv_oc_y.append(t_l)
    if t_h: hv_oc_x.append(fc); hv_oc_y.append(t_h)

fig.add_trace(go.Scatter(x=fdr_oc_x, y=fdr_oc_y, name="Zone 1: 11kV Feeder 51", line=dict(color="#d32f2f", width=2.5)), row=1, col=1)
fig.add_trace(go.Scatter(x=lv_oc_x, y=lv_oc_y, name="Zone 2: 11kV PTR LV 51", line=dict(color="#0288d1", width=2.5)), row=1, col=1)
fig.add_trace(go.Scatter(x=hv_oc_x, y=hv_oc_y, name="Zone 3: 33kV PTR HV 51 (Ref 11kV)", line=dict(color="#388e3c", width=2.5, dash="dash")), row=1, col=1)

fig.add_vline(x=max_3ph, line_dash="solid", line_color="black", annotation_text=f"PTR %Z Max 3Ph: {max_3ph}A", row=1, col=1)
if hs_50_fdr_vs_lv:
    fig.add_vline(x=hs_50_fdr_vs_lv, line_dash="dot", line_color="orange", annotation_text=f"Feeder 50 High-Set: {hs_50_fdr_vs_lv}A", row=1, col=1)

# --- Plot 2: Earth Fault (51N / 51G + Dyn11 33kV Phase 51 Sensitivity) ---
step_ef = max(2, int(max_ef / 120))
fc_ef_range = list(range(5, max_ef + step_ef, step_ef))
fdr_ef_x, fdr_ef_y, lv_ef_x, lv_ef_y, hv_lg_x, hv_lg_y = [], [], [], [], [], []

for fc in fc_ef_range:
    t_f_ef = calc_idmt_time(fc, fdr_ef_pickup, fdr_ef_tms, c_fdr)
    t_l_ef = calc_idmt_time(fc, lv_ef_pickup, lv_ef_tms, c_lv)
    # 33kV Phase 51 response to an 11kV L-G fault (compensated for Dyn11 sqrt(3) factor)
    t_h_lg = calc_idmt_time(fc, hv_oc_pickup_referred_11kv_lg, hv_oc_tms, c_hv)
    if t_f_ef: fdr_ef_x.append(fc); fdr_ef_y.append(t_f_ef)
    if t_l_ef: lv_ef_x.append(fc); lv_ef_y.append(t_l_ef)
    if t_h_lg: hv_lg_x.append(fc); hv_lg_y.append(t_h_lg)

fig.add_trace(go.Scatter(x=fdr_ef_x, y=fdr_ef_y, name="Zone 1: 11kV Feeder 51N", line=dict(color="#d32f2f", width=2.5, dash="dot")), row=1, col=2)
fig.add_trace(go.Scatter(x=lv_ef_x, y=lv_ef_y, name="Zone 2: PTR Neutral 51G", line=dict(color="#0288d1", width=2.5, dash="dot")), row=1, col=2)
fig.add_trace(go.Scatter(x=hv_lg_x, y=hv_lg_y, name="Zone 3: 33kV 51 (Dyn11 L-G Reflected)", line=dict(color="#388e3c", width=2, dash="dashdot")), row=1, col=2)

fig.add_vline(x=max_ef, line_dash="solid", line_color="purple", annotation_text=f"NGR Max EF: {max_ef}A", row=1, col=2)
if hs_50n_fdr_vs_lv:
    fig.add_vline(x=hs_50n_fdr_vs_lv, line_dash="dot", line_color="orange", annotation_text=f"50N High-Set: {hs_50n_fdr_vs_lv}A", row=1, col=2)

fig.update_xaxes(type="log", title_text="Fault Current Referred to 11kV Base (A)")
fig.update_yaxes(type="log", title_text="Operating Time (s)", range=[-1.5, 2.0])
fig.update_layout(height=580, legend=dict(orientation="h", yanchor="bottom", y=-0.25, xanchor="center", x=0.5))

st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------
# 6. SELECTIVITY AUDIT TABLE ACROSS FAULT LEVELS
# ---------------------------------------------------------
st.subheader("📋 3-Zone Coordination & Selectivity Audit Table")

test_points = sorted(list(set([
    int(ptr_data["i_fl_lv_A"] * 1.5),
    int(ptr_data["max_ef_lv_A"]),
    int(ptr_data["max_3ph_sc_lv_A"] * 0.5),
    int(ptr_data["max_3ph_sc_lv_A"])
])))

audit_rows = []
for fc in test_points:
    t1_oc = calc_idmt_time(fc, fdr_oc_pickup, fdr_oc_tms, c_fdr)
    t2_oc = calc_idmt_time(fc, lv_oc_pickup, lv_oc_tms, c_lv)
    t3_oc = calc_idmt_time(fc, hv_oc_pickup_referred_11kv_3ph, hv_oc_tms, c_hv)

    cti_12_ms = (t2_oc - t1_oc) * 1000.0 if (t1_oc and t2_oc) else None
    cti_23_ms = (t3_oc - t2_oc) * 1000.0 if (t2_oc and t3_oc) else None

    status = "✅ PASS (Selective)"
    if (cti_12_ms is not None and cti_12_ms < total_cti_sec * 1000.0) or (cti_23_ms is not None and cti_23_ms < total_cti_sec * 1000.0):
        status = "⚠️ Enable ANSI 50 High-Set"

    audit_rows.append({
        "11kV Fault Current (A)": fc,
        "Reflected 33kV 3Ph Current (A)": round(fc / ptr_data["turn_ratio"], 1),
        "Reflected 33kV Dyn11 L-G Current (A)": round(fc * ptr_data["dyn11_lg_hv_factor"], 1),
        "Zone 1 Feeder 51 (s)": round(t1_oc, 3) if t1_oc else "No Pickup",
        "Zone 2 PTR LV 51 (s)": round(t2_oc, 3) if t2_oc else "No Pickup",
        "Zone 3 PTR HV 51 (s)": round(t3_oc, 3) if t3_oc else "No Pickup",
        "Margin Z2-Z1 (ms)": round(cti_12_ms, 1) if cti_12_ms else "N/A",
        "Margin Z3-Z2 (ms)": round(cti_23_ms, 1) if cti_23_ms else "N/A",
        "Audit Verdict": status
    })

df_audit = pd.DataFrame(audit_rows)
st.dataframe(df_audit, use_container_width=True)

csv_bytes = df_audit.to_csv(index=False).encode("utf-8")
st.download_button(
    label=f"📥 Download {substation_name} Coordination Audit CSV",
    data=csv_bytes,
    file_name=f"{substation_name}_3Zone_Relay_Audit.csv",
    mime="text/csv"
)
