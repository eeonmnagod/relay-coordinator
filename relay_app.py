import streamlit as st
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import os

# --- APP SETUP & STATE MANAGEMENT ---
st.set_page_config(page_title="IDMT & High-Set Grading", layout="wide")

if 'accepted_terms' not in st.session_state:
    st.session_state.accepted_terms = False

if not st.session_state.accepted_terms:
    st.markdown("""
    <style>.stApp { background-color: #0E1117; color: #FAFAFA; } .st-emotion-cache-16txtl3 { padding: 2rem 1.5rem; background-color: #262730; }</style>
    """, unsafe_allow_html=True)
    
    st.title("⚡ Protection Relay Coordination Tool")
    st.markdown("### Welcome, Pallav.")
    st.markdown("""
    **New Feature:** High-Set (50) Instantaneous element grading has been integrated. The app now calculates the exact Pick-up Current needed for Instantaneous tripping to maintain a 100ms margin across all PSM levels.
    """)
    if st.button("I Accept and Understand"):
        st.session_state.accepted_terms = True
        st.rerun()
    st.stop()

# --- DATABASE LOGIC ---
DATA_FILE = "relay_database.csv"

def load_db():
    if os.path.exists(DATA_FILE):
        return pd.read_csv(DATA_FILE)
    return pd.DataFrame(columns=[
        "Substation", "Feeder", "Voltage", "CT (A)", 
        "Fault (A)", "Pick-up (A)", "Curve", "TMS", "Op Time (s)", "Inst (A)", "Inst Time (s)"
    ])

def save_db(dataframe):
    dataframe.to_csv(DATA_FILE, index=False)

# --- CORE MATH LOGIC ---
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

def calc_inst_pickup(target_time, tms, pickup_current, curve_constant):
    """Calculates the fault current at which IDMT hits the target time limit"""
    if target_time <= 0 or not tms or not pickup_current:
        return np.nan
    try:
        val = (curve_constant * tms / target_time) + 1
        psm = val ** 50  # Math inverse of ^0.02
        return psm * pickup_current
    except OverflowError:
        return np.nan

# --- UI INJECTION ---
st.sidebar.header("🎨 UI Settings")
dark_mode = st.sidebar.toggle("🌙 Enable Dark Mode", value=True)
if dark_mode:
    st.markdown("""<style>.stApp { background-color: #0E1117; color: #FAFAFA; } div[data-testid="stMetricValue"] { color: #4DD0E1; }</style>""", unsafe_allow_html=True)
    plot_template = "plotly_dark"
else:
    plot_template = "plotly_white"

st.title("⚡ Substation Relay Time Grading (IDMT + Inst)")
db_df = load_db()

# --- 1. FEEDER IDENTIFICATION ---
st.sidebar.header("1. Feeder Identification")
substation = st.sidebar.text_input("Substation Name", placeholder="e.g. Nainpur")
feeder = st.sidebar.text_input("Feeder Name", placeholder="e.g. 11kV Main")
voltage_level = st.sidebar.selectbox("Voltage Level", ["132 kV", "33 kV", "11 kV"])
ct_rating = st.sidebar.number_input("CT Rating (Primary Amps)", min_value=10.0, value=None, step=50.0)

# --- 2. IDMT PARAMETERS ---
st.sidebar.header("2. IDMT (51) Parameters")
curve_type = st.sidebar.selectbox("Curve Type", list(CURVE_CONSTANTS.keys()))
pickup_current = st.sidebar.number_input("IDMT Pick-up Current (A)", min_value=1.0, value=None, step=10.0)

# --- 3. TMS & GRADING LOGIC ---
st.sidebar.header("3. Time Grading & TMS")
tms_mode = st.sidebar.radio("TMS Mode", ["Manual Entry", "Auto-Grade from Downstream"])
tms = None
target_margin_time = 0.100 # Default 100ms
fault_current = None

if tms_mode == "Manual Entry":
    fault_current = st.sidebar.number_input("Expected Fault Current (A)", min_value=10.0, value=None, step=100.0)
    tms = st.sidebar.number_input("TMS", min_value=0.01, value=None, step=0.01)
else:
    if not db_df.empty:
        ds_options = [f"{row['Substation']} - {row['Feeder']}" for _, row in db_df.iterrows()]
        selected_ds = st.sidebar.selectbox("Select Downstream Relay", ds_options)
        
        # Load downstream relay data
        ds_sub, ds_feed = selected_ds.split(" - ", 1)
        ds_relay = db_df[(db_df['Substation'] == ds_sub) & (db_df['Feeder'] == ds_feed)].iloc[0]
        ds_c_const = CURVE_CONSTANTS["Standard Inverse (1.3 Sec)"] if "1.3" in ds_relay["Curve"] else CURVE_CONSTANTS["Very Inverse (3.0 Sec)"]
        
        # Determine the worst-case fault current for grading
        # If downstream has an instantaneous element, grade exactly at its Instant Pick-up
        if pd.notna(ds_relay.get("Inst (A)")) and ds_relay["Inst (A)"] > 0:
            fault_current = st.sidebar.number_input("Fault Current (Auto-Selected from Downstream Inst)", value=float(ds_relay["Inst (A)"]), disabled=True)
            ds_time = ds_relay["Inst Time (s)"]
        else:
            fault_current = st.sidebar.number_input("Expected Fault Current (A)", min_value=10.0, value=3000.0, step=100.0)
            ds_psm = calc_psm(fault_current, ds_relay["Pick-up (A)"])
            ds_time = calc_time(ds_psm, ds_relay["TMS"], ds_c_const)
            
        if pickup_current:
            if not np.isnan(ds_time):
                target_time = ds_time + target_margin_time
                current_psm = calc_psm(fault_current, pickup_current)
                if current_psm > 1.0:
                    tms = calc_required_tms(target_time, current_psm, CURVE_CONSTANTS[curve_type])
                    st.sidebar.success(f"Required TMS: {tms:.4f}")
                else:
                    st.sidebar.error("Current PSM <= 1. Will not trip.")
            else:
                st.sidebar.error("Downstream relay does not operate.")
    else:
        st.sidebar.warning("Save a relay to the database first.")

# --- 4. HIGH-SET (INSTANTANEOUS) ELEMENT ---
st.sidebar.header("4. High-Set (50) Element")
enable_inst = st.sidebar.checkbox("Enable Instantaneous Tripping")
inst_pickup = None
inst_time = None

if enable_inst:
    inst_time = st.sidebar.number_input("Inst Trip Time (s)", min_value=0.01, value=0.03, step=0.01)
    
    # Calculate recommended Instantaneous Pick-up based on IDMT curve
    recommended_inst = np.nan
    if tms and pickup_current:
        # We calculate the fault current where the IDMT curve crosses 100ms
        target_inst_margin = 0.100
        recommended_inst = calc_inst_pickup(target_inst_margin, tms, pickup_current, CURVE_CONSTANTS[curve_type])
        
        if not np.isnan(recommended_inst):
            st.sidebar.info(f"💡 **Recommended Inst Pick-up:** {recommended_inst:.0f} A\n*(Maintains 100ms grading limit)*")
            
    inst_pickup = st.sidebar.number_input("Inst Pick-up (A)", min_value=10.0, value=float(recommended_inst) if not np.isnan(recommended_inst) else None, step=100.0)

# --- DASHBOARD METRICS ---
current_psm = np.nan
current_op_time = np.nan

if fault_current and pickup_current:
    current_psm = calc_psm(fault_current, pickup_current)
    if tms:
        current_op_time = calc_time(current_psm, tms, CURVE_CONSTANTS[curve_type])
        # Override with Instantaneous time if fault exceeds threshold
        if enable_inst and inst_pickup and fault_current >= inst_pickup:
            current_op_time = inst_time

disp_feeder = feeder if feeder else "Pending Feeder"
st.markdown(f"### Current Configuration: **{disp_feeder}**")
col1, col2, col3, col4 = st.columns(4)
col1.metric("Calculated PSM", f"{current_psm:.2f}" if not np.isnan(current_psm) else "--")
col2.metric("Calculated TMS", f"{tms:.4f}" if tms else "--")
col3.metric("Operating Time", f"{current_op_time:.3f} Sec" if not np.isnan(current_op_time) else "--")
col4.metric("Inst Pick-up", f"{inst_pickup:.0f} A" if inst_pickup else "Disabled")

# Save to Database Button
if st.button("💾 Add Relay to Database", type="primary"):
    if None in [substation, feeder, ct_rating, fault_current, pickup_current, tms] or substation == "" or feeder == "":
        st.error("⚠️ Please fill out required parameters (Substation, Feeder, CT, IDMT Pick-up, TMS).")
    else:
        new_row = pd.DataFrame([{
            "Substation": substation,
            "Feeder": feeder,
            "Voltage": voltage_level,
            "CT (A)": ct_rating,
            "Fault (A)": fault_current,
            "Pick-up (A)": pickup_current,
            "Curve": "1.3s" if "1.3" in curve_type else "3.0s",
            "TMS": round(tms, 4),
            "Op Time (s)": round(current_op_time, 3) if not np.isnan(current_op_time) else "No Trip",
            "Inst (A)": round(inst_pickup, 1) if inst_pickup else np.nan,
            "Inst Time (s)": inst_time if inst_pickup else np.nan
        }])
        updated_db = pd.concat([db_df, new_row], ignore_index=True)
        save_db(updated_db)
        st.success("Added to database!")
        st.rerun()

st.divider()

# --- INTERACTIVE DATABASE TABLE ---
st.subheader("Coordination & Time Grading Database")
if not db_df.empty:
    edited_df = st.data_editor(db_df, num_rows="dynamic", use_container_width=True)
    if st.button("🔄 Commit Table Changes to Database"):
        save_db(edited_df)
        st.success("Database updated successfully!")
        st.rerun()

# --- MULTI-CURVE PLOTTING WITH INSTANTANEOUS DROP ---
st.subheader("TCC Coordination Plot")
if not db_df.empty:
    fig = go.Figure()
    # Generate base array of current ranges
    plot_currents = np.linspace(100, 20000, 1000) 
    
    for _, row in db_df.iterrows():
        c_const = CURVE_CONSTANTS["Standard Inverse (1.3 Sec)"] if row["Curve"] == "1.3s" else CURVE_CONSTANTS["Very Inverse (3.0 Sec)"]
        
        x_vals = []
        y_vals = []
        
        has_inst = pd.notna(row.get("Inst (A)")) and row["Inst (A)"] > 0
        inst_a = row.get("Inst (A)")
        inst_t = row.get("Inst Time (s)")

        for fc in plot_currents:
            if has_inst and fc > inst_a:
                continue # Handled below to ensure sharp visual drop
                
            t_idmt = calc_time(calc_psm(fc, row["Pick-up (A)"]), row["TMS"], c_const)
            x_vals.append(fc)
            y_vals.append(t_idmt)

        if has_inst:
            # Draw the exact vertical drop for the Instantaneous setting
            t_idmt_at_inst = calc_time(calc_psm(inst_a, row["Pick-up (A)"]), row["TMS"], c_const)
            x_vals.append(inst_a)
            y_vals.append(t_idmt_at_inst) # Top of drop
            
            x_vals.append(inst_a)
            y_vals.append(inst_t)         # Bottom of drop
            
            x_vals.append(max(plot_currents))
            y_vals.append(inst_t)         # Flat line out to max fault current
            
        fig.add_trace(go.Scatter(
            x=x_vals, 
            y=y_vals, 
            mode='lines', 
            name=f'{row["Substation"]} - {row["Feeder"]}'
        ))

    fig.update_layout(
        template=plot_template,
        xaxis_title="Fault Current (Amps)",
        yaxis_title="Operating Time (Seconds)",
        yaxis_type="log",
        xaxis_type="log",
        hovermode="x unified",
        height=650
    )
    st.plotly_chart(fig, use_container_width=True)
