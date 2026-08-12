import streamlit as st
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import os

# --- APP SETUP & STATE MANAGEMENT ---
st.set_page_config(page_title="IDMT Time Grading App", layout="wide")

# 1. LANDING PAGE & ACCEPTANCE GATE
if 'accepted_terms' not in st.session_state:
    st.session_state.accepted_terms = False

if not st.session_state.accepted_terms:
    # Inject dark mode for landing page
    st.markdown("""
    <style>
        .stApp { background-color: #0E1117; color: #FAFAFA; }
        .st-emotion-cache-16txtl3 { padding: 2rem 1.5rem; background-color: #262730; }
    </style>
    """, unsafe_allow_html=True)
    
    st.title("⚡ Protection Relay Coordination Tool")
    st.markdown("### Welcome,")
    st.markdown("""
    This application is designed to calculate Inverse Definite Minimum Time (IDMT) relay settings, verify 100ms coordination margins, and generate Time-Current Characteristic (TCC) curves for field deployment.
    
    **Instructions for Use:**
    1. **Add Relays:** Use the sidebar menu to input Substation and Feeder parameters.
    2. **Calculate:** Enter the Expected Fault Current and Pick-up Current.
    3. **Auto-Grade:** Use the "Auto-Calculate" mode to automatically generate a TMS that maintains a 100ms margin above a downstream relay.
    4. **Database:** Click "Add Relay to Database" to save your configuration. Your data will persist even if you close the browser.
    5. **Manage Data:** In the main table, you can double-click cells to edit them, or select a row's checkbox (on the far left) and press your **Delete** key to remove it. Click the "Update Database" button below the table to commit your changes.
    """)
    
    st.warning("⚠️ **Safety Notice:** This tool provides mathematical TCC grading. All settings must be verified against actual field hardware specifications before physical deployment.")
    
    if st.button("I Accept and Understand the Instructions"):
        st.session_state.accepted_terms = True
        st.rerun()
    
    st.stop() # Halts the script here until the user accepts

# --- DATABASE LOGIC ---
DATA_FILE = "relay_database.csv"

def load_db():
    if os.path.exists(DATA_FILE):
        return pd.read_csv(DATA_FILE)
    return pd.DataFrame(columns=[
        "Substation", "Feeder", "Voltage", "CT (A)", 
        "Fault (A)", "Pick-up (A)", "Curve", "TMS", "Op Time (s)"
    ])

def save_db(dataframe):
    dataframe.to_csv(DATA_FILE, index=False)

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

# Load existing data for reference
db_df = load_db()

# --- SIDEBAR INPUTS ---
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
    if not db_df.empty:
        downstream_options = [f"{row['Substation']} - {row['Feeder']}" for _, row in db_df.iterrows()]
        selected_ds_name = st.sidebar.selectbox("Select Downstream Relay", downstream_options)
        
        if fault_current and pickup_current:
            # Locate the selected downstream relay in the dataframe
            ds_sub, ds_feed = selected_ds_name.split(" - ", 1)
            ds_relay = db_df[(db_df['Substation'] == ds_sub) & (db_df['Feeder'] == ds_feed)].iloc[0]
            
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
        st.sidebar.warning("Save at least one feeder to the database first to use Auto-Calculate.")

# --- CURRENT CALCULATION ---
current_psm = np.nan
current_op_time = np.nan

if fault_current and pickup_current:
    current_psm = calc_psm(fault_current, pickup_current)
    if tms:
        current_op_time = calc_time(current_psm, tms, CURVE_CONSTANTS[curve_type])

disp_feeder = feeder if feeder else "Pending Feeder"
disp_sub = substation if substation else "Pending Substation"

st.markdown(f"### Current Configuration: **{disp_feeder}** ({disp_sub})")
col1, col2, col3 = st.columns(3)
col1.metric("Calculated PSM", f"{current_psm:.2f}" if not np.isnan(current_psm) else "--")
col2.metric("Operating Time", f"{current_op_time:.3f} Sec" if not np.isnan(current_op_time) else "--")
col3.metric("Calculated TMS", f"{tms:.4f}" if tms else "--")

# Save to Database Button
if st.sidebar.button("💾 Add Relay to Database", type="primary"):
    if None in [substation, feeder, ct_rating, fault_current, pickup_current, tms] or substation == "" or feeder == "":
        st.sidebar.error("⚠️ Please fill out all parameters.")
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
            "Op Time (s)": round(current_op_time, 3) if not np.isnan(current_op_time) else "No Trip"
        }])
        updated_db = pd.concat([db_df, new_row], ignore_index=True)
        save_db(updated_db)
        st.sidebar.success("Added to database!")
        st.rerun()

st.divider()

# --- INTERACTIVE DATABASE TABLE ---
st.subheader("Coordination & Time Grading Database")
if not db_df.empty:
    st.markdown("✏️ *You can edit cells directly. To **delete a row**, select the checkbox on its left edge and press the `Delete` key on your keyboard.*")
    
    # st.data_editor allows dynamic row addition/deletion
    edited_df = st.data_editor(db_df, num_rows="dynamic", use_container_width=True, key="data_editor")
    
    # Button to commit table edits or deletions back to the CSV
    if st.button("🔄 Commit Table Changes to Database"):
        save_db(edited_df)
        st.success("Database updated successfully!")
        st.rerun()
else:
    st.info("Database is empty. Configure a relay on the left and click 'Add Relay to Database'.")

# --- MULTI-CURVE PLOTTING ---
st.subheader("TCC Coordination Plot")
if not db_df.empty:
    fig = go.Figure()
    plot_currents = np.linspace(100, 15000, 500)
    
    for _, row in db_df.iterrows():
        c_const = CURVE_CONSTANTS["Standard Inverse (1.3 Sec)"] if row["Curve"] == "1.3s" else CURVE_CONSTANTS["Very Inverse (3.0 Sec)"]
        times = [calc_time(calc_psm(fc, row["Pick-up (A)"]), row["TMS"], c_const) for fc in plot_currents]
        
        fig.add_trace(go.Scatter(
            x=plot_currents, 
            y=times, 
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
        height=600
    )
    st.plotly_chart(fig, use_container_width=True)
