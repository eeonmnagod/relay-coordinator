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
    if pickup_current == 0:
        return 0
    return fault_current / pickup_current

def calc_time(psm, tms, curve_constant):
    if psm <= 1.0:
        return np.nan 
    return (curve_constant / ((psm ** 0.02) - 1)) * tms

def calc_required_tms(target_time, psm, curve_constant):
    if psm <= 1.0:
        return 0.0
    return target_time / (curve_constant / ((psm ** 0.02) - 1))

# --- APP SETUP & STATE MANAGEMENT ---
st.set_page_config(page_title="IDMT Time Grading App", layout="wide")
st.title("⚡ Substation Relay Time Grading")

if 'grading_table' not in st.session_state:
    st.session_state.grading_table = []

# --- SIDEBAR INPUTS ---
st.sidebar.header("1. Feeder Identification")
substation = st.sidebar.text_input("Substation Name", value="Nainpur")
feeder = st.sidebar.text_input("Feeder Name", value="33kV I/C")
voltage_level = st.sidebar.selectbox("Voltage Level", ["132 kV", "33 kV", "11 kV"])
ct_rating = st.sidebar.number_input("CT Rating (Primary Amps)", min_value=10.0, value=400.0, step=50.0)

st.sidebar.header("2. Relay Parameters")
fault_current = st.sidebar.number_input("Expected Fault Current (Amps)", min_value=10.0, value=3000.0, step=100.0)
pickup_current = st.sidebar.number_input("Pick-up Current (Amps)", min_value=1.0, value=200.0, step=10.0)
curve_type = st.sidebar.selectbox("Curve Type", list(CURVE_CONSTANTS.keys()))

st.sidebar.header("3. TMS Configuration")
tms_mode = st.sidebar.radio("Setting Mode", ["Manual Entry", "Auto-Calculate (100ms Margin)"])

tms = 0.10 # Default fallback
if tms_mode == "Manual Entry":
    tms = st.sidebar.number_input("Time Multiplier Setting (TMS)", min_value=0.01, value=0.10, step=0.01)
else:
    if st.session_state.grading_table:
        # Create a list of saved relays for the user to select as the downstream reference
        downstream_options = [f"{r['Substation']} - {r['Feeder']}" for r in st.session_state.grading_table]
        selected_ds_name = st.sidebar.selectbox("Select Downstream Relay", downstream_options)
        
        # Retrieve the selected relay's data
        ds_relay = next(r for r in st.session_state.grading_table if f"{r['Substation']} - {r['Feeder']}" == selected_ds_name)
        ds_c_const = CURVE_CONSTANTS["Standard Inverse (1.3 Sec)"] if ds_relay["Curve"] == "1.3s" else CURVE_CONSTANTS["Very Inverse (3.0 Sec)"]
        
        # Calculate downstream time at the CURRENT expected fault current
        ds_psm = calc_psm(fault_current, ds_relay["Pick-up (A)"])
        ds_time = calc_time(ds_psm, ds_relay["TMS"], ds_c_const)
        
        if not np.isnan(ds_time):
            target_time = ds_time + 0.100 # Add 100ms coordination margin
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

# --- CURRENT CALCULATION ---
current_psm = calc_psm(fault_current, pickup_current)
current_op_time = calc_time(current_psm, tms, CURVE_CONSTANTS[curve_type])

st.markdown(f"### Current Configuration: **{feeder}** ({substation})")
col1, col2, col3 = st.columns(3)
col1.metric("Calculated PSM", f"{current_psm:.2f}")
col2.metric("Operating Time", f"{current_op_time:.3f} Sec" if not np.isnan(current_op_time) else "No Trip")
col3.metric("Calculated TMS", f"{tms:.4f}")

# Save Button
if st.button("💾 Save to Grading Table"):
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
    st.info("No relays saved yet. Adjust parameters on the left and click 'Save to Grading Table'.")

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
        xaxis_title="Fault Current (Amps)",
        yaxis_title="Operating Time (Seconds)",
        yaxis_type="log",
        xaxis_type="log",
        hovermode="x unified",
        height=600
    )
    st.plotly_chart(fig, use_container_width=True)
