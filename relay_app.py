import streamlit as st
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import os

# --- APP SETUP ---
st.set_page_config(page_title="DISCOM Relay Coordination", layout="wide")

st.title("⚡ Protection Relay Time Grading (DISCOM Standard)")
st.markdown("""
This version utilizes standard DISCOM logic. Enter your relay parameters, and use the interactive TCC graph to trace operating times. 
**New Feature:** Use the Margin Analytics tool below the table to automatically calculate the maximum allowable fault current before your 100ms coordination breaks.
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

# --- CORE MATH LOGIC ---
CURVE_CONSTANTS = {
    "1.3 Sec curve": 0.0607,
    "3.0 Sec curve": 0.14
}

def calc_time(fault_current, pickup_current, tms, curve_constant):
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

# --- NUMERICAL HIGH-SET CALCULATOR (Bisection Method) ---
def calculate_high_set_limit(up_row, down_row, margin=0.100, max_fc=50000):
    down_v = parse_voltage(down_row["Voltage"])
    up_v = parse_voltage(up_row["Voltage"])
    trans_ratio = down_v / up_v # Ratio to reflect downstream fault to upstream relay
    
    c_down = CURVE_CONSTANTS.get(down_row["Curve"], 0.0607)
    c_up = CURVE_CONSTANTS.get(up_row["Curve"], 0.0607)

    def get_margin(I_down):
        I_up = I_down * trans_ratio
        t_d = calc_time(I_down, down_row["Pick-up (A)"], down_row["TMS"], c_down)
        t_u = calc_time(I_up, up_row["Pick-up (A)"], up_row["TMS"], c_up)
        if np.isnan(t_d) or np.isnan(t_u):
            return np.nan
        return t_u - t_d

    # Find where both relays are operating
    min_i_down = down_row["Pick-up (A)"] * 1.05
    min_i_up_referred = (up_row["Pick-up (A)"] / trans_ratio) * 1.05
    low = max(min_i_down, min_i_up_referred)
    high = max_fc

    m_low = get_margin(low)
    m_high = get_margin(high)

    if np.isnan(m_low):
        return None, "Relays do not operate at low fault currents. Check Pick-up values."
    if m_low < margin:
        return None, f"Grading Failed: Margin is already {m_low*100:.0f}ms (below 100ms) near pick-up."
    if m_high > margin:
        return None, f"Margin remains > 100ms up to {max_fc} A. No High-Set required."

    # Binary search for the exact 100ms crossing point
    for _ in range(100):
        mid = (low + high) / 2
        m_mid = get_margin(mid)
        if np.isnan(m_mid):
            break
        if m_mid > margin:
            low = mid # Gap is too big, move to higher currents
        else:
            high = mid # Gap is too small, move to lower currents
        if abs(m_mid - margin) < 0.001:
            break
            
    return mid, f"⚠️ Margin drops below 100ms at {mid:.0f} A"


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
col_table, col_calc = st.columns([2, 1])

with col_table:
    st.subheader("Coordination Database")
    if not db_df.empty:
        st.markdown("✏️ *Double-click cells to edit. Select a row's checkbox to delete it.*")
        edited_df = st.data_editor(db_df, num_rows="dynamic", use_container_width=True)
        if st.button("🔄 Commit Table Changes"):
            save_db(edited_df)
            st.success("Database updated successfully!")
            st.rerun()
    else:
        st.info("The database is empty. Add a relay configuration to view the TCC plot.")

# --- MARGIN ANALYTICS & HIGH-SET CALCULATOR ---
with col_calc:
    st.subheader("🔍 Margin Analytics")
    st.markdown("Calculate the exact fault current where the 100ms grading gap breaks.")
    
    if not db_df.empty and len(db_df) >= 2:
        relay_options = [f"{row['Substation']} - {row['Feeder']} ({row['Voltage']})" for _, row in db_df.iterrows()]
        
        up_selection = st.selectbox("Select Upstream Relay", relay_options, index=0)
        down_selection = st.selectbox("Select Downstream Relay", relay_options, index=1)
        
        if st.button("Calculate High-Set Limit"):
            up_idx = relay_options.index(up_selection)
            down_idx = relay_options.index(down_selection)
            
            up_row = db_df.iloc[up_idx]
            down_row = db_df.iloc[down_idx]
            
            limit_amps, message = calculate_high_set_limit(up_row, down_row)
            
            if limit_amps:
                st.error(message)
                st.markdown(f"""
                **Recommendation:**
                Set the Instantaneous (High-Set) pick-up on **{down_row['Feeder']}** to **≤ {limit_amps:.0f} A**. 
                If a fault exceeds this value, the IDMT delay is bypassed, ensuring it clears before the upstream relay reacts.
                """)
                
                # Store the limit in session state so we can draw it on the plot
                st.session_state['hs_limit'] = limit_amps
                st.session_state['hs_down_v'] = parse_voltage(down_row["Voltage"])
            else:
                st.info(message)
                if 'hs_limit' in st.session_state:
                    del st.session_state['hs_limit']
    else:
        st.warning("Save at least two relays to the database to use this feature.")

st.divider()

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
        c_const = CURVE_CONSTANTS.get(row["Curve"], 0.0607) 
        
        x_vals = []
        y_vals = []

        for fc_base in base_plot_currents:
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

    # If the user calculated a High-Set limit, draw it on the plot
    if 'hs_limit' in st.session_state:
        # Reflect the limit to the current chart base voltage
        hs_limit_base = st.session_state['hs_limit'] * (st.session_state['hs_down_v'] / base_v_val)
        
        fig.add_vline(
            x=hs_limit_base, 
            line_dash="dash", 
            line_color="red", 
            annotation_text=" 100ms Margin Break Limit", 
            annotation_position="top right",
            annotation_font_color="red"
        )

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
