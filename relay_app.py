import streamlit as st
import numpy as np
import pandas as pd
import plotly.graph_objects as go

# --- CORE LOGIC (From Spreadsheet) ---
CURVE_CONSTANTS = {
    "Standard Inverse (1.3 Sec)": 0.0607,
    "Very Inverse (3.0 Sec)": 0.14
}

def calc_psm(fault_current, pickup_current):
    """Calculate Plug Setting Multiplier (PSM)"""
    if pickup_current == 0:
        return 0
    return fault_current / pickup_current

def calc_time(psm, tms, curve_constant):
    """Calculate Relay Operating Time"""
    if psm <= 1.0:
        return np.nan # Relay will not operate
    return (curve_constant / ((psm ** 0.02) - 1)) * tms

def calc_required_tms(target_time, psm, curve_constant):
    """Calculate required TMS to achieve a specific operating time"""
    if psm <= 1.0:
        return 0.0
    return target_time / (curve_constant / ((psm ** 0.02) - 1))

# --- WEB APP UI ---
st.set_page_config(page_title="IDMT Relay Coordination App", layout="wide")
st.title("⚡ Relay Coordination & Setting Calculator")
st.markdown("Calculate IDMT relay operating times and ensure a minimum 100ms margin between main and feeder relays.")

# Sidebar for Inputs
st.sidebar.header("System Parameters")
fault_current = st.sidebar.number_input("Expected Fault Current (Amps)", min_value=10.0, value=3000.0, step=100.0)

st.sidebar.subheader("Upstream Relay (Main / I/C)")
main_pickup = st.sidebar.number_input("Main Pick-up Current (Amps)", value=200.0)
main_curve = st.sidebar.selectbox("Main Curve Type", list(CURVE_CONSTANTS.keys()), key="main_curve")
main_tms = st.sidebar.number_input("Main TMS", min_value=0.01, value=0.10, step=0.01)

st.sidebar.subheader("Downstream Relay (Feeder)")
feeder_pickup = st.sidebar.number_input("Feeder Pick-up Current (Amps)", value=150.0)
feeder_curve = st.sidebar.selectbox("Feeder Curve Type", list(CURVE_CONSTANTS.keys()), key="feeder_curve")

# --- CALCULATIONS ---
# Main Relay calculations
main_psm = calc_psm(fault_current, main_pickup)
main_op_time = calc_time(main_psm, main_tms, CURVE_CONSTANTS[main_curve])

# Feeder Relay logic (Targeting a 100ms / 0.1s margin)
target_feeder_time = main_op_time - 0.100 if not np.isnan(main_op_time) else np.nan
feeder_psm = calc_psm(fault_current, feeder_pickup)

if not np.isnan(target_feeder_time) and target_feeder_time > 0:
    required_feeder_tms = calc_required_tms(target_feeder_time, feeder_psm, CURVE_CONSTANTS[feeder_curve])
else:
    required_feeder_tms = 0.05 # Default minimum

# --- DISPLAY RESULTS ---
col1, col2, col3 = st.columns(3)
col1.metric("Main Relay Time", f"{main_op_time:.3f} Sec" if not np.isnan(main_op_time) else "No Trip")
col2.metric("Target Feeder Time (-100ms)", f"{target_feeder_time:.3f} Sec" if not np.isnan(target_feeder_time) else "N/A")
col3.metric("Required Feeder TMS", f"{required_feeder_tms:.4f}")

# --- PLOTTING TCC CURVES ---
st.subheader("Time-Current Characteristic (TCC) Curves")

# Generate an array of fault currents for the plot (100A to 15000A)
plot_currents = np.linspace(100, 15000, 500)
main_times = [calc_time(calc_psm(fc, main_pickup), main_tms, CURVE_CONSTANTS[main_curve]) for fc in plot_currents]
feeder_times = [calc_time(calc_psm(fc, feeder_pickup), required_feeder_tms, CURVE_CONSTANTS[feeder_curve]) for fc in plot_currents]

# Create interactive plot
fig = go.Figure()
fig.add_trace(go.Scatter(x=plot_currents, y=main_times, mode='lines', name='Upstream Main (I/C)', line=dict(color='red')))
fig.add_trace(go.Scatter(x=plot_currents, y=feeder_times, mode='lines', name='Downstream Feeder', line=dict(color='blue')))

# Add a vertical line for the specific calculated fault current
fig.add_vline(x=fault_current, line_dash="dash", line_color="green", annotation_text="Expected Fault Current")

fig.update_layout(
    xaxis_title="Fault Current (Amps)",
    yaxis_title="Operating Time (Seconds)",
    yaxis_type="log", # Log scale is standard for TCC curves
    xaxis_type="log",
    hovermode="x unified"
)

st.plotly_chart(fig, use_container_width=True)
