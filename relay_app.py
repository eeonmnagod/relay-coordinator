import streamlit as st
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import os

# --- APP SETUP ---
st.set_page_config(page_title="DISCOM Relay Coordination", layout="wide")

st.title("⚡ Protection Relay Time Grading (DISCOM Standard)")
st.markdown("""
This version utilizes standard DISCOM logic. 
The static fault current inputs and auto-calculations have been removed. Instead, **the math does the work in the curve plotting**. 
Simply enter your relay parameters, and use the interactive TCC graph to trace operating times across all fault currents to visually verify your 100ms coordination margins.
""")

# --- DATABASE LOGIC ---
DATA_FILE = "discom_relay_database.csv"

def load_db():
    if os.path.exists(DATA_FILE):
        return pd.read_csv(DATA_FILE)
    return pd.DataFrame(columns=[
        "Substation", "Feeder", "Voltage", "CT (A)", "Pick-up (A)", "Curve", "TMS"
    ])

def save_db(dataframe):
    dataframe.to_csv(DATA_FILE, index=False)

db_df = load_db()

# --- CORE MATH LOGIC (From Spreadsheet) ---
CURVE_CONSTANTS = {
    "1.3 Sec curve": 0.0607,
    "3.0 Sec curve": 0.14
}

def calc_time(fault_current, pickup_current, tms, curve_constant):
    """Calculates Operating Time: (a / ((PSM^0.02) - 1)) * TMS"""
    if pickup_current == 0:
        return np.nan
    
    psm = fault_current / pickup_current
    
    if psm <= 1.0 or not tms:
        return np.nan
        
    return (curve_constant / ((psm ** 0.02) - 1)) * tms

def parse_voltage(v_str):
    try:
        return float(v_str.lower().replace("kv", "").strip())
    except:
        return 1.0

# --- UI INJECTION ---
st.sidebar.header("🎨 UI Settings")
dark_mode = st.sidebar.toggle("🌙 Enable Dark Mode", value=True)
if dark_mode:
    st.markdown("""<style>.stApp { background-color: #0E1117; color: #FAFAFA; }</style>""", unsafe_allow_html=True)
    plot_template = "plotly_dark"
else:
    plot_template = "plotly_white"

# --- SIDEBAR INPUTS ---
st.sidebar.header("1. Feeder Details")
substation = st.sidebar.text_input("Substation Name", placeholder="e.g. Nainpur", value=None)
feeder = st.sidebar.text_input("Feeder Name", placeholder="e.g. 33kV PTR", value=None)
voltage_level = st.sidebar.selectbox("Voltage Level", ["132 kV", "33 kV", "11 kV"])
ct_rating = st.sidebar.number_input("CT Rating (Primary Amps)", min_value=10.0, step=50.0, value=None)

st.sidebar.header("2. Relay Operating Parameters")
curve_type = st.sidebar.selectbox("Curve Type", list(CURVE_CONSTANTS.keys()))
pickup_current = st.sidebar.number_input("Pick-up Current (A) [y]", min_value=1.0, step=10.0, value=None)
tms = st.sidebar.number_input("TMS", min_value=0.01, step=0.01, value=None)

# Save to Database Button
if st.sidebar.button("💾 Add Relay to Database", type="primary"):
    if None in [substation, feeder, ct_rating, pickup_current, tms] or substation == "" or feeder == "":
        st.sidebar.error("⚠️ Please fill out all parameters.")
    else:
        new_row = pd.DataFrame([{
            "Substation": substation, "Feeder": feeder, "Voltage": voltage_level,
            "CT (A)": ct_rating, "Pick-up (A)": pickup_current,
            "Curve": curve_type, "TMS": round(tms, 4)
        }])
        updated_db = pd.concat([db_df, new_row], ignore_index=True)
        save_db(updated_db)
        st.sidebar.success("Added to database!")
        st.rerun()

st.divider()

# --- DATABASE TABLE ---
st.subheader("Coordination Database")
if not db_df.empty:
    st.markdown("✏️ *Double-click cells to edit. Select a row's checkbox to delete it.*")
    edited_df = st.data_editor(db_df, num_rows="dynamic", use_container_width=True)
    if st.button("🔄 Commit Table Changes"):
        save_db(edited_df)
        st.success("Database updated successfully!")
        st.rerun()

# --- MULTI-CURVE PLOTTING ---
st.subheader("TCC Coordination Plot")
if not db_df.empty:
    
    colA, colB = st.columns([1, 3])
    plot_base_str = colA.selectbox("Chart Base Voltage (For visual grading)", ["11 kV", "33 kV", "132 kV"], index=0)
    base_v_val = parse_voltage(plot_base_str)
    
    fig = go.Figure()
    
    # Generate a logarithmic array of fault currents [x]
    base_plot_currents = np.logspace(np.log10(10), np.log10(25000), 1000) 
    
    for _, row in db_df.iterrows():
        row_v_val = parse_voltage(row["Voltage"])
        
        # Fallback in case old curve names are still in the CSV
        c_const = CURVE_CONSTANTS.get(row["Curve"], 0.0607) 
        
        x_vals = []
        y_vals = []

        for fc_base in base_plot_currents:
            # Reflect fault current based on voltage level
            fc_relay = fc_base * (base_v_val / row_v_val)
            
            t_idmt = calc_time(fc_relay, row["Pick-up (A)"], row["TMS"], c_const)
            
            if not np.isnan(t_idmt):
                x_vals.append(fc_base)
                y_vals.append(t_idmt)

        fig.add_trace(go.Scatter(
            x=x_vals, 
            y=y_vals, 
            mode='lines', 
            name=f'{row["Substation"]} - {row["Feeder"]} ({row["Voltage"]})',
            hovertemplate="Fault: %{x:.0f} A<br>Time: %{y:.3f} s<extra></extra>"
        ))

    fig.update_layout(
        template=plot_template,
        xaxis_title=f"Fault Current (Amps) - Referred to {plot_base_str} Base",
        yaxis_title="Operating Time (Seconds)",
        yaxis_type="log",
        xaxis_type="log",
        hovermode="x unified",
        height=700,
        legend=dict(yanchor="top", y=0.99, xanchor="right", x=0.99, bgcolor="rgba(0,0,0,0.5)")
    )
    
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("The database is empty. Add a relay configuration to view the TCC plot.")
