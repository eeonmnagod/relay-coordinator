import streamlit as st
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import os

# --- APP SETUP ---
st.set_page_config(page_title="DISCOM Relay Coordination", layout="wide")

st.title("⚡ Protection Relay Time Grading (OC & EF)")
st.markdown("""
**Hierarchical Constraint Mode:** 
This tool now enforces the "Hard Ceiling Rule". A downstream relay's High-Set will automatically be capped by its immediate upstream relay's High-Set (referred across voltage levels) to prevent racing and maintain selectivity.
""")

# --- DATABASE LOGIC ---
DATA_FILE = "discom_relay_database.csv"

def load_db():
    if os.path.exists(DATA_FILE):
        return pd.read_csv(DATA_FILE)
    return pd.DataFrame(columns=[
        "Substation", "Feeder", "Voltage", "CT (A)", "Curve",
        "OC Pick-up (A)", "OC TMS", "OC Inst (A)", 
        "EF Pick-up (A)", "EF TMS", "EF Inst (A)"
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
    if not pickup_current or pickup_current == 0 or pd.isna(pickup_current):
        return np.nan
    psm = fault_current / pickup_current
    if psm <= 1.0 or not tms or pd.isna(tms):
        return np.nan
    return (curve_constant / ((psm ** 0.02) - 1)) * tms

def parse_voltage(v_str):
    try:
        return float(v_str.lower().replace("kv", "").strip())
    except:
        return 1.0

# --- NUMERICAL HIGH-SET CALCULATOR (WITH CEILING CONSTRAINTS) ---
def calculate_high_set_limit(up_row, down_row, fault_type="OC", margin=0.100, max_fc=50000):
    down_v = parse_voltage(down_row["Voltage"])
    up_v = parse_voltage(up_row["Voltage"])
    trans_ratio = down_v / up_v 
    
    c_down = CURVE_CONSTANTS.get(down_row["Curve"], 0.0607)
    c_up = CURVE_CONSTANTS.get(up_row["Curve"], 0.0607)
    
    down_pu = down_row[f"{fault_type} Pick-up (A)"]
    down_tms = down_row[f"{fault_type} TMS"]
    up_pu = up_row[f"{fault_type} Pick-up (A)"]
    up_tms = up_row[f"{fault_type} TMS"]

    if pd.isna(down_pu) or pd.isna(up_pu):
        return None, None, None, trans_ratio, "Pick-up parameters missing."

    # 1. Calculate Mathematical Convergence Limit (100ms break)
    def get_margin(I_down):
        I_up = I_down * trans_ratio
        t_d = calc_time(I_down, down_pu, down_tms, c_down)
        t_u = calc_time(I_up, up_pu, up_tms, c_up)
        if np.isnan(t_d) or np.isnan(t_u): return np.nan
        return t_u - t_d

    min_i_down = down_pu * 1.05
    min_i_up_referred = (up_pu / trans_ratio) * 1.05
    low = max(min_i_down, min_i_up_referred)
    high = max_fc
    math_limit = None
    msg = ""

    m_low = get_margin(low)
    m_high = get_margin(high)

    if np.isnan(m_low):
        msg = "Relays do not operate at low currents."
    elif m_low < margin:
        msg = f"Grading Fails early (margin {m_low*100:.0f}ms at pick-up)."
    elif m_high > margin:
        math_limit = max_fc
        msg = f"Curves never converge < 100ms within {max_fc}A."
    else:
        for _ in range(100):
            mid = (low + high) / 2
            m_mid = get_margin(mid)
            if np.isnan(m_mid): break
            if m_mid > margin: low = mid 
            else: high = mid 
            if abs(m_mid - margin) < 0.001: break
        math_limit = mid
        msg = f"Mathematical convergence at {math_limit:.0f} A."

    # 2. Check Upstream Ceiling Constraint
    upstream_inst = up_row.get(f"{fault_type} Inst (A)")
    ceiling_limit = None
    if pd.notna(upstream_inst) and upstream_inst > 0:
        # Refer the upstream limit to the downstream voltage side
        ceiling_limit = upstream_inst / trans_ratio

    # 3. Apply the strictest constraint
    final_limit = math_limit
    if ceiling_limit is not None and (math_limit is None or ceiling_limit < math_limit):
        final_limit = ceiling_limit
        msg = f"Capped by Upstream High-Set limit."

    return final_limit, math_limit, ceiling_limit, trans_ratio, msg


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
substation = st.sidebar.text_input("Substation Name", placeholder="e.g. Chhindwara", value=None)
feeder = st.sidebar.text_input("Feeder Name", placeholder="e.g. 33kV PTR", value=None)
voltage_level = st.sidebar.selectbox("Voltage Level", ["132 kV", "33 kV", "11 kV"])
ct_rating = st.sidebar.number_input("CT Rating (Primary Amps)", min_value=10.0, step=50.0, value=None)
curve_type = st.sidebar.selectbox("Curve Type", list(CURVE_CONSTANTS.keys()))

st.sidebar.header("2. Phase Overcurrent (OC)")
oc_pickup = st.sidebar.number_input("OC Pick-up (A)", min_value=1.0, step=5.0, value=None)
oc_tms = st.sidebar.number_input("OC TMS", min_value=0.01, step=0.01, value=None)
oc_inst = st.sidebar.number_input("OC Inst / High-Set (A) [Optional]", min_value=10.0, step=10.0, value=None)

st.sidebar.header("3. Earth Fault (EF)")
ef_pickup = st.sidebar.number_input("EF Pick-up (A)", min_value=1.0, step=1.0, value=None)
ef_tms = st.sidebar.number_input("EF TMS", min_value=0.01, step=0.01, value=None)
ef_inst = st.sidebar.number_input("EF Inst / High-Set (A) [Optional]", min_value=10.0, step=10.0, value=None)

# Save to Database Button
if st.sidebar.button("💾 Add Relay to Database", type="primary"):
    if None in [substation, feeder, ct_rating] or substation == "" or feeder == "":
        st.sidebar.error("⚠️ Please fill out Substation, Feeder, and CT Rating.")
    else:
        new_row = pd.DataFrame([{
            "Substation": substation, "Feeder": feeder, "Voltage": voltage_level,
            "CT (A)": ct_rating, "Curve": curve_type,
            "OC Pick-up (A)": oc_pickup, "OC TMS": round(oc_tms, 4) if oc_tms else None, "OC Inst (A)": oc_inst,
            "EF Pick-up (A)": ef_pickup, "EF TMS": round(ef_tms, 4) if ef_tms else None, "EF Inst (A)": ef_inst
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
    st.markdown("Calculate safe High-Set constraints based on Math Limits & Upstream Ceilings.")
    
    if not db_df.empty and len(db_df) >= 2:
        relay_options = [f"{row['Substation']} - {row['Feeder']} ({row['Voltage']})" for _, row in db_df.iterrows()]
        
        up_selection = st.selectbox("Select Upstream Relay", relay_options, index=0)
        down_selection = st.selectbox("Select Downstream Relay", relay_options, index=1)
        
        if st.button("Calculate High-Set Limits", type="primary"):
            up_idx = relay_options.index(up_selection)
            down_idx = relay_options.index(down_selection)
            
            up_row = db_df.iloc[up_idx]
            down_row = db_df.iloc[down_idx]
            
            limit_oc, math_oc, ceil_oc, t_ratio, msg_oc = calculate_high_set_limit(up_row, down_row, fault_type="OC")
            limit_ef, math_ef, ceil_ef, _, msg_ef = calculate_high_set_limit(up_row, down_row, fault_type="EF")
            
            if t_ratio != 1.0:
                st.info(f"🔄 **Transformer Reflection:** Downstream faults are multiplied by **{t_ratio:.3f}**.")
            elif up_row['Voltage'] == down_row['Voltage']:
                st.info(f"⚡ **Same Voltage Bus:** E.g., 33kV Outgoing graded against a downstream 33kV Substation Incoming VCB. No transformer reflection applied.")

            st.markdown("---")
            st.markdown("### Phase Overcurrent (OC)")
            if math_oc: st.write(f"📉 **Math Limit:** {math_oc:.0f} A")
            if ceil_oc: st.write(f"🛑 **Upstream Ceiling:** {ceil_oc:.0f} A")
            
            if limit_oc:
                st.error(f"**Required Setting:** Set OC High-Set ≤ **{limit_oc:.0f} A**")
                st.caption(msg_oc)
                st.session_state['hs_limit_oc'] = limit_oc
            else:
                st.warning(msg_oc)
                
            st.markdown("---")
            st.markdown("### Earth Fault (EF)")
            if math_ef: st.write(f"📉 **Math Limit:** {math_ef:.0f} A")
            if ceil_ef: st.write(f"🛑 **Upstream Ceiling:** {ceil_ef:.0f} A")
            
            if limit_ef:
                if limit_oc and limit_ef > limit_oc:
                    practical_limit_ef = limit_oc
                    st.error(f"**Required Setting:** Set EF High-Set ≤ **{practical_limit_ef:.0f} A**")
                    st.caption("*(EF constrained mathematically, but further capped by the practical OC limit).*")
                    st.session_state['hs_limit_ef'] = practical_limit_ef
                else:
                    st.error(f"**Required Setting:** Set EF High-Set ≤ **{limit_ef:.0f} A**")
                    st.caption(msg_ef)
                    st.session_state['hs_limit_ef'] = limit_ef
            else:
                st.warning(msg_ef)
                
            st.session_state['hs_down_v'] = parse_voltage(down_row["Voltage"])
            
    else:
        st.warning("Save at least two relays to the database to use this feature.")

st.divider()

# --- MULTI-CURVE PLOTTING ---
st.subheader("TCC Coordination Plot")
if not db_df.empty:
    
    colA, colB = st.columns([1, 3])
    plot_base_str = colA.selectbox("Chart Base Voltage (For visual grading)", ["11 kV", "33 kV", "132 kV"], index=0)
    plot_element = colB.radio("Element to Plot", ["Phase Overcurrent (OC)", "Earth Fault (EF)"], horizontal=True)
    
    base_v_val = parse_voltage(plot_base_str)
    fault_type_key = "OC" if "OC" in plot_element else "EF"
    
    fig = go.Figure()
    base_plot_currents = np.logspace(np.log10(10), np.log10(25000), 1000) 
    
    for _, row in db_df.iterrows():
        row_v_val = parse_voltage(row["Voltage"])
        c_const = CURVE_CONSTANTS.get(row["Curve"], 0.0607) 
        
        pu_val = row[f"{fault_type_key} Pick-up (A)"]
        tms_val = row[f"{fault_type_key} TMS"]
        inst_val = row[f"{fault_type_key} Inst (A)"]
        
        if pd.isna(pu_val) or pd.isna(tms_val):
            continue 
        
        x_vals = []
        y_vals = []
        
        inst_val_base = inst_val * (row_v_val / base_v_val) if pd.notna(inst_val) and inst_val > 0 else None

        for fc_base in base_plot_currents:
            if inst_val_base and fc_base > inst_val_base:
                continue # Bypass IDMT curve plotting beyond Instantaneous setting
                
            fc_relay = fc_base * (base_v_val / row_v_val)
            t_idmt = calc_time(fc_relay, pu_val, tms_val, c_const)
            
            if not np.isnan(t_idmt):
                x_vals.append(fc_base)
                y_vals.append(t_idmt)

        # Plot Instantaneous Drop Visually
        if inst_val_base and inst_val_base in base_plot_currents:
            t_idmt_at_inst = calc_time(inst_val_base * (base_v_val / row_v_val), pu_val, tms_val, c_const)
            x_vals.append(inst_val_base)
            y_vals.append(t_idmt_at_inst) 
            x_vals.append(inst_val_base)
            y_vals.append(0.03) # Approximate 30ms Instantaneous breaker time
            x_vals.append(max(base_plot_currents))
            y_vals.append(0.03)

        fig.add_trace(go.Scatter(
            x=x_vals, 
            y=y_vals, 
            mode='lines', 
            name=f'{row["Substation"]} - {row["Feeder"]} ({row["Voltage"]})',
            hovertemplate="Fault: %{x:.0f} A<br>Time: %{y:.3f} s<extra></extra>"
        ))

    limit_key = 'hs_limit_oc' if fault_type_key == "OC" else 'hs_limit_ef'
    if limit_key in st.session_state:
        hs_limit_base = st.session_state[limit_key] * (st.session_state['hs_down_v'] / base_v_val)
        
        fig.add_vline(
            x=hs_limit_base, 
            line_dash="dash", 
            line_color="red", 
            annotation_text=f" Rec. {fault_type_key} High-Set Limit", 
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
