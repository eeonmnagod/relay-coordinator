import streamlit as st
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import os

# --- APP SETUP & STATE MANAGEMENT ---
st.set_page_config(page_title="IDMT Grading & Transformer Reflection", layout="wide")

if 'accepted_terms' not in st.session_state:
    st.session_state.accepted_terms = False

if not st.session_state.accepted_terms:
    st.markdown("""<style>.stApp { background-color: #0E1117; color: #FAFAFA; } .st-emotion-cache-16txtl3 { padding: 2rem 1.5rem; background-color: #262730; }</style>""", unsafe_allow_html=True)
    st.title("⚡ Protection Relay Coordination Tool")
    st.markdown("### Welcome, Pallav.")
    st.markdown("""
    **New Feature:** Transformer Reflection & Common Base Plotting. 
    The app now automatically applies the voltage transformation ratio (e.g., $11/33 = 0.333$) when grading across power transformers, ensuring the upstream relay calculates its TMS based on the *actual* reflected fault current it sees.
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

def parse_voltage(v_str):
    """Extracts numeric voltage from string (e.g., '33 kV' -> 33.0)"""
    try:
        return float(v_str.lower().replace("kv", "").strip())
    except:
        return 1.0

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
    if target_time <= 0 or not tms or not pickup_current:
        return np.nan
    try:
        val = (curve_constant * tms / target_time) + 1
        return (val ** 50) * pickup_current
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

st.title("⚡ Substation Relay Time Grading")
db_df = load_db()

# --- 1. FEEDER IDENTIFICATION ---
st.sidebar.header("1. Feeder Identification")
substation = st.sidebar.text_input("Substation Name", placeholder="e.g. Nainpur")
feeder = st.sidebar.text_input("Feeder Name", placeholder="e.g. 33kV PTR")
voltage_level = st.sidebar.selectbox("Voltage Level", ["132 kV", "33 kV", "11 kV"])
ct_rating = st.sidebar.number_input("CT Rating (Primary Amps)", min_value=10.0, value=None, step=50.0)

# --- 2. IDMT PARAMETERS ---
st.sidebar.header("2. IDMT (51) Parameters")
curve_type = st.sidebar.selectbox("Curve Type", list(CURVE_CONSTANTS.keys()))
pickup_current = st.sidebar.number_input("IDMT Pick-up Current (A)", min_value=1.0, value=None, step=10.0)

# --- 3. TMS & GRADING LOGIC ---
st.sidebar.header("3. Time Grading & TMS")
tms_mode = st.sidebar.radio("TMS Mode", ["Manual Entry", "Auto-Coordinate (100ms Margin)"])

coord_dir = st.sidebar.radio(
    "Coordination Direction", 
    ["Towards Downstream (This relay is Upstream)", "Towards Upstream (This relay is Downstream)"],
    disabled=(tms_mode == "Manual Entry")
)

tms = None
target_margin_time = 0.100 
fault_current = None
current_v_val = parse_voltage(voltage_level)

if tms_mode == "Manual Entry":
    fault_current = st.sidebar.number_input("Expected Fault Current (A)", min_value=10.0, value=None, step=100.0)
    tms = st.sidebar.number_input("TMS", min_value=0.01, value=None, step=0.01)
else:
    if not db_df.empty:
        ds_options = [f"{row['Substation']} - {row['Feeder']}" for _, row in db_df.iterrows()]
        selected_ref = st.sidebar.selectbox("Select Reference Relay", ds_options)
        
        ref_sub, ref_feed = selected_ref.split(" - ", 1)
        ref_relay = db_df[(db_df['Substation'] == ref_sub) & (db_df['Feeder'] == ref_feed)].iloc[0]
        ref_v_val = parse_voltage(ref_relay["Voltage"])
        ref_c_const = CURVE_CONSTANTS["Standard Inverse (1.3 Sec)"] if "1.3" in ref_relay["Curve"] else CURVE_CONSTANTS["Very Inverse (3.0 Sec)"]
        
        # Determine the transformation ratio
        trans_ratio = ref_v_val / current_v_val
        
        if pd.notna(ref_relay.get("Inst (A)")) and ref_relay["Inst (A)"] > 0:
            auto_fault = float(ref_relay["Inst (A)"])
            ref_time = float(ref_relay["Inst Time (s)"])
            st.sidebar.info(f"📊 Reference Inst Pick-up: **{auto_fault} A** at {ref_relay['Voltage']}")
        else:
            auto_fault = float(ref_relay["Fault (A)"])
            ref_psm = calc_psm(auto_fault, ref_relay["Pick-up (A)"])
            ref_time = calc_time(ref_psm, ref_relay["TMS"], ref_c_const)
            st.sidebar.info(f"📊 Reference Fault Current: **{auto_fault} A** at {ref_relay['Voltage']}")

        # Reflect the fault current across the transformer to this relay's voltage level
        referred_fault = auto_fault * trans_ratio
        
        if trans_ratio != 1.0:
            st.sidebar.warning(f"🔄 **Transformer Reflection:**\nFault current multiplied by ({ref_v_val}/{current_v_val}) = **{trans_ratio:.3f}**")
            
        fault_current = st.sidebar.number_input("Reflected Grading Fault Current (A)", value=referred_fault, disabled=True)
            
        if pickup_current:
            if not np.isnan(ref_time):
                target_time = ref_time + target_margin_time if "Towards Downstream" in coord_dir else ref_time - target_margin_time
                
                if target_time <= 0:
                    st.sidebar.error("Target time <= 0. Upstream relay trips too fast to coordinate against.")
                else:
                    current_psm = calc_psm(fault_current, pickup_current)
                    if current_psm > 1.0:
                        tms = calc_required_tms(target_time, current_psm, CURVE_CONSTANTS[curve_type])
                        st.sidebar.success(f"Required TMS: {tms:.4f} (Target Trip: {target_time:.3f}s)")
                    else:
                        st.sidebar.error("Current PSM <= 1. Will not trip.")
            else:
                st.sidebar.error("Reference relay does not operate.")
    else:
        st.sidebar.warning("⚠️ Save at least one relay to the database first.")
        fault_current = st.sidebar.number_input("Grading Fault Current (A)", value=None, disabled=True)

# --- 4. HIGH-SET (INSTANTANEOUS) ELEMENT ---
st.sidebar.header("4. High-Set (50) Element")
enable_inst = st.sidebar.checkbox("Enable Instantaneous Tripping")
inst_pickup = None
inst_time = None

if enable_inst:
    inst_time = st.sidebar.number_input("Inst Trip Time (s)", min_value=0.01, value=0.03, step=0.01)
    
    recommended_inst = np.nan
    if tms and pickup_current:
        recommended_inst = calc_inst_pickup(0.100, tms, pickup_current, CURVE_CONSTANTS[curve_type])
        if not np.isnan(recommended_inst):
            st.sidebar.info(f"💡 **Recommended Inst Pick-up:** {recommended_inst:.0f} A")
            
    inst_pickup = st.sidebar.number_input("Inst Pick-up (A)", min_value=10.0, value=float(recommended_inst) if not np.isnan(recommended_inst) else None, step=100.0)

# --- DASHBOARD METRICS ---
current_psm = np.nan
current_op_time = np.nan

if fault_current and pickup_current:
    current_psm = calc_psm(fault_current, pickup_current)
    if tms:
        current_op_time = calc_time(current_psm, tms, CURVE_CONSTANTS[curve_type])
        if enable_inst and inst_pickup and fault_current >= inst_pickup:
            current_op_time = inst_time

disp_feeder = feeder if feeder else "Pending Feeder"
st.markdown(f"### Current Configuration: **{disp_feeder}**")
col1, col2, col3, col4 = st.columns(4)
col1.metric("Calculated PSM", f"{current_psm:.2f}" if not np.isnan(current_psm) else "--")
col2.metric("Calculated TMS", f"{tms:.4f}" if tms else "--")
col3.metric("Operating Time", f"{current_op_time:.3f} Sec" if not np.isnan(current_op_time) else "--")
col4.metric("Inst Pick-up", f"{inst_pickup:.0f} A" if inst_pickup else "Disabled")

if st.button("💾 Add Relay to Database", type="primary"):
    if None in [substation, feeder, ct_rating, fault_current, pickup_current, tms] or substation == "" or feeder == "":
        st.error("⚠️ Please fill out required parameters.")
    else:
        new_row = pd.DataFrame([{
            "Substation": substation, "Feeder": feeder, "Voltage": voltage_level,
            "CT (A)": ct_rating, "Fault (A)": fault_current, "Pick-up (A)": pickup_current,
            "Curve": "1.3s" if "1.3" in curve_type else "3.0s", "TMS": round(tms, 4),
            "Op Time (s)": round(current_op_time, 3) if not np.isnan(current_op_time) else "No Trip",
            "Inst (A)": round(inst_pickup, 1) if inst_pickup else np.nan,
            "Inst Time (s)": inst_time if inst_pickup else np.nan
        }])
        updated_db = pd.concat([db_df, new_row], ignore_index=True)
        save_db(updated_db)
        st.success("Added to database!")
        st.rerun()

st.divider()

# --- DATABASE TABLE ---
st.subheader("Coordination & Time Grading Database")
if not db_df.empty:
    edited_df = st.data_editor(db_df, num_rows="dynamic", use_container_width=True)
    if st.button("🔄 Commit Table Changes to Database"):
        save_db(edited_df)
        st.success("Database updated successfully!")
        st.rerun()

# --- MULTI-CURVE PLOTTING WITH BASE VOLTAGE SHIFT ---
st.subheader("TCC Coordination Plot")
if not db_df.empty:
    
    colA, colB = st.columns([1, 3])
    plot_base_str = colA.selectbox("Chart Base Voltage", ["11 kV", "33 kV", "132 kV"], index=0)
    base_v_val = parse_voltage(plot_base_str)
    
    fig = go.Figure()
    # Plot array based on the chosen Chart Base Voltage
    base_plot_currents = np.linspace(10, 25000, 1000) 
    
    for _, row in db_df.iterrows():
        row_v_val = parse_voltage(row["Voltage"])
        c_const = CURVE_CONSTANTS["Standard Inverse (1.3 Sec)"] if row["Curve"] == "1.3s" else CURVE_CONSTANTS["Very Inverse (3.0 Sec)"]
        
        x_vals = []
        y_vals = []
        has_inst = pd.notna(row.get("Inst (A)")) and row["Inst (A)"] > 0
        inst_a_relay = row.get("Inst (A)")
        inst_a_base = inst_a_relay * (row_v_val / base_v_val) if has_inst else None
        inst_t = row.get("Inst Time (s)")

        for fc_base in base_plot_currents:
            if has_inst and fc_base > inst_a_base:
                continue 
            
            # What current does the relay actually see for this base current?
            fc_relay = fc_base * (base_v_val / row_v_val)
            t_idmt = calc_time(calc_psm(fc_relay, row["Pick-up (A)"]), row["TMS"], c_const)
            
            if not np.isnan(t_idmt):
                x_vals.append(fc_base)
                y_vals.append(t_idmt)

        if has_inst and inst_a_base in base_plot_currents:
            # Connect IDMT curve to Instantaneous drop
            t_idmt_at_inst = calc_time(calc_psm(inst_a_relay, row["Pick-up (A)"]), row["TMS"], c_const)
            x_vals.append(inst_a_base)
            y_vals.append(t_idmt_at_inst) 
            x_vals.append(inst_a_base)
            y_vals.append(inst_t)         
            x_vals.append(max(base_plot_currents))
            y_vals.append(inst_t)         
            
        fig.add_trace(go.Scatter(
            x=x_vals, 
            y=y_vals, 
            mode='lines', 
            name=f'{row["Substation"]} - {row["Feeder"]} ({row["Voltage"]})'
        ))

    fig.update_layout(
        template=plot_template,
        xaxis_title=f"Fault Current (Amps) - Referred to {plot_base_str} Base",
        yaxis_title="Operating Time (Seconds)",
        yaxis_type="log",
        xaxis_type="log",
        hovermode="x unified",
        height=650
    )
    st.plotly_chart(fig, use_container_width=True)
