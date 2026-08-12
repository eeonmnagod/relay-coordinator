import streamlit as st
import numpy as np
import pandas as pd
import plotly.graph_objects as go

# --- CORE LOGIC ---
CURVE_CONSTANTS = {
    "Standard Inverse (1.3 Sec)": 0.0607,
    "Very Inverse (3.0 Sec)": 0.14
}

def calc_psm(fault_current, pickup_current):
    if not pickup_current or pickup_current == 0:
        return 0
    return fault_current / pickup_current

def calc_time(psm, tms, curve_constant):
    if psm <= 1.0 or not tms:
        return np.nan 
    return (curve_constant / ((psm ** 0.02) - 1)) * tms

def calc_required_tms(target_time, psm, curve_constant):
    if psm <= 1.0:
        return 0.0
    return target_time / (curve_constant / ((psm ** 0.02) - 1))

# --- APP SETUP & STATE MANAGEMENT ---
st.set_page_config(page_title="IDMT Time Grading App", layout="wide")

if 'grading_table' not in st.session_state:
    st.session_state.grading_table = []

# --- DARK MODE UI INJECTION ---
st.sidebar.header("🎨 UI Settings")
dark_mode = st.sidebar.toggle("🌙 Enable Dark Mode", value=True)

if dark_mode:
    dark_css = """
    <style>
        .stApp { background-color: #0E1117; color: #FAFAFA; }
        .st-emotion-cache-16txtl3 { padding: 2rem 1.5rem; background-color: #262730; }
        div[data-testid="stMetricValue"] { color: #4DD0E1; }
    </style>
    """
    st.markdown(dark_css, unsafe_allow_html=True)
    plot_template = "plotly_dark"
else:
    plot_template = "plotly_white"

st.title("⚡ Substation Relay Time Grading")

# --- SIDEBAR INPUTS (Blank by Default) ---
st.sidebar.header("1. Feeder Identification")
substation = st.sidebar.text_input("Substation Name", value=None, placeholder="e.g. Nainpur")
feeder = st.sidebar.text_input("Feeder Name", value=None, placeholder="e.g. 11kV Main")
voltage_level = st.sidebar.selectbox("Voltage Level", ["132 kV", "33 kV", "11 kV"])
ct_rating = st.sidebar.number_input("CT Rating (Primary Amps)", min_value=10.0, value=None, step=50.0, placeholder="e.g. 400")

st.sidebar.header("2. Relay Parameters")
fault_current = st.sidebar.number_input("Expected Fault Current (Amps)", min_value=10.0, value=None, step=100.0, placeholder="e.g. 3000")
pickup_current = st.sidebar.number_input("Pick-up Current (Amps)", min_value=1.0, value=None, step=10.0, placeholder="e.g. 200")
curve_type = st.sidebar.selectbox("Curve Type", list(CURVE_CONSTANTS.keys()))

st.sidebar.header("3. TMS Configuration")
tms_mode = st.sidebar.radio("Setting Mode", ["Manual Entry", "Auto-Calculate (100ms Margin)"])

tms = None
if tms_mode == "Manual Entry":
    tms = st.sidebar.number_input("Time Multiplier Setting (TMS)", min_value=0.01, value=None, step=0.01, placeholder="e.g. 0.10")
else:
    if st.session_state.grading_table:
        downstream_options = [f"{r['Substation']} - {r['Feeder']}" for r in st.session_state.grading_table]
        selected_ds_name = st.sidebar.selectbox("Select Downstream Relay", downstream_options)
        
        if fault_current and pickup_current:
            ds_relay = next(r for r in st.session_state.grading_table if f"{r['Substation']} - {r['Feeder']}" == selected_ds_name)
            ds_c_const = CURVE_CONSTANTS["Standard Inverse (1.3 Sec)"] if ds_relay["Curve"] == "1.3s" else CURVE_CONSTANTS["Very Inverse (3.0 Sec)"]
            
            ds_psm = calc_psm(fault_current, ds_relay["Pick-up (A)"])
            ds_time = calc_time(ds_psm, ds_relay["TMS"], ds_c_const)
            
            if not np.isnan(ds_time):
                target_time = ds_time + 0.100 
                current_psm = calc_psm(fault_current, pickup_current)
                
                if current_psm > 1.0:
                    tms = calc_required_tms(target_time, current_psm, CURVE_CONSTANTS[curve_type])
                    st.sidebar.success(f"Required TMS: {tms:.4f}")
                else:
                    st.sidebar.error("Current PSM <= 1. Relay will not operate.")
            else:
                st.sidebar.error("Downstream relay does not operate at this fault current.")
    else:
        st.sidebar.warning("Save at least one feeder to the table first to use Auto-Calculate.")

# --- CURRENT CALCULATION (With Safety Checks) ---
current_psm = np.nan
current_op_time = np.nan

if fault_current and pickup_current:
    current_psm = calc_psm(fault_current, pickup_current)
    if tms:
        current_op_time = calc_time(current_psm, tms, CURVE_CONSTANTS[curve_type])

# Use fallback display names if fields are empty
disp_feeder = feeder if feeder else "Pending Feeder"
disp_sub = substation if substation else "Pending Substation"

st.markdown(f"### Current Configuration: **{disp_feeder}** ({disp_sub})")
col1, col2, col3 = st.columns(3)
col1.metric("Calculated PSM", f"{current_psm:.2f}" if not np.isnan(current_psm) else "--")
col2.metric("Operating Time", f"{current_op_time:.3f} Sec" if not np.isnan(current_op_time) else "--")
col3.metric("Calculated TMS", f"{tms:.4f}" if tms else "--")

# Save Button (Disabled if missing data)
if st.button("💾 Save to Grading Table"):
    if None in [substation, feeder, ct_rating, fault_current, pickup_current, tms] or substation == "" or feeder == "":
        st.error("⚠️ Please fill out all Feeder and Relay Parameters before saving.")
    else:
        st.session_state.grading_table.append({
            "Substation": substation,
            "Feeder": feeder,
            "Voltage": voltage_level,
            "CT (A)": ct_rating,
            "Fault (A)": fault_current,
            "Pick-up (A)": pickup_current,
            "Curve": "1.3s" if "1.3" in curve_type else "3.0s",
            "TMS": round(tms, 4),
            "Op Time (s)": round(current_op_time, 3) if not np.isnan(current_op_time) else "No Trip"
        })
        st.success(f"Saved {feeder} to the coordination table.")

st.divider()

# --- GRADING TABLE ---
st.subheader("Coordination & Time Grading Table")
if st.session_state.grading_table:
    df = pd.DataFrame(st.session_state.grading_table)
    st.dataframe(df, use_container_width=True)
    
    if st.button("🗑️ Clear All Saved Data"):
        st.session_state.grading_table = []
        st.rerun()
else:
    st.info("No relays saved yet. Fill out the parameters on the left and click 'Save to Grading Table'.")

# --- MULTI-CURVE PLOTTING ---
st.subheader("TCC Coordination Plot")
if st.session_state.grading_table:
    fig = go.Figure()
    plot_currents = np.linspace(100, 15000, 500)
    
    for entry in st.session_state.grading_table:
        c_const = CURVE_CONSTANTS["Standard Inverse (1.3 Sec)"] if entry["Curve"] == "1.3s" else CURVE_CONSTANTS["Very Inverse (3.0 Sec)"]
        times = [calc_time(calc_psm(fc, entry["Pick-up (A)"]), entry["TMS"], c_const) for fc in plot_currents]
        
        fig.add_trace(go.Scatter(
            x=plot_currents, 
            y=times, 
            mode='lines', 
            name=f'{entry["Substation"]} - {entry["Feeder"]}'
        ))

    fig.update_layout(
        template=plot_template,
        xaxis_title="Fault Current (Amps)",
        yaxis_title="Operating Time (Seconds)",
        yaxis_type="log",
        xaxis_type="log",
        hovermode="x unified",
        height=600
    )
    st.plotly_chart(fig, use_container_width=True)
