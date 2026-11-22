import streamlit as st
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import os
import re
import math
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# --- APP SETUP ---
st.set_page_config(page_title="DISCOM Relay Coordination", layout="wide")

st.title("⚡ Protection Relay Time Grading (OC & EF)")
st.markdown("""
**Hierarchical Constraint Mode (with Google Sheet Import, PTR NGR & %Z Fault Ceilings):** 
Enforces the "Hard Ceiling Rule". Imports field relay data directly from `Form Responses 1` (coalescing all conditional division/substation sections and converting CT % settings into Primary Amps) and caps downstream High-Sets by upstream limits, PTR `%Z` 3-phase limits, and PTR Neutral Earth Resistance ($R_{\\text{NGR}}$).
""")

# --- DATABASE & GOOGLE SHEETS LOGIC ---
DATA_FILE = "discom_relay_database.csv"
GSHEET_URL = "https://docs.google.com/spreadsheets/d/1jzdbWPRC-zKd7qeQ9d5_eimUjqwYd5LbtCtTxmuhmOQ/edit?usp=sharing"
SERVICE_ACCOUNT_FILE = "service_account.json"

COLUMNS = [
    "Division", "Substation", "Feeder", "Voltage", "CT (A)", "Curve",
    "PTR MVA", "PTR %Z", "PTR NGR (Ohms)", "Max NGR EF (A)",
    "OC Pick-up (A)", "OC TMS", "OC Inst (A)", 
    "EF Pick-up (A)", "EF TMS", "EF Inst (A)"
]

FLOAT_COLUMNS = [
    "CT (A)", "PTR MVA", "PTR %Z", "PTR NGR (Ohms)", "Max NGR EF (A)",
    "OC Pick-up (A)", "OC TMS", "OC Inst (A)", 
    "EF Pick-up (A)", "EF TMS", "EF Inst (A)"
]

def enforce_float_dtypes(df):
    """Ensures all numerical columns (especially OC TMS and EF TMS) remain strict float64."""
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = np.nan
    for f_col in FLOAT_COLUMNS:
        df[f_col] = pd.to_numeric(df[f_col], errors="coerce").astype("float64")
    return df[COLUMNS]

def load_db():
    if os.path.exists(DATA_FILE):
        df = pd.read_csv(DATA_FILE)
        return enforce_float_dtypes(df)
    return enforce_float_dtypes(pd.DataFrame(columns=COLUMNS))

def save_db(dataframe):
    clean_df = enforce_float_dtypes(dataframe.copy())
    clean_df.to_csv(DATA_FILE, index=False)

def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    if "gcp_service_account" in st.secrets:
        creds = Credentials.from_service_account_info(
            st.secrets["gcp_service_account"], scopes=scopes
        )
        return gspread.authorize(creds)
    elif os.path.exists(SERVICE_ACCOUNT_FILE):
        creds = Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=scopes
        )
        return gspread.authorize(creds)
    else:
        raise FileNotFoundError(
            "Google credentials not found. Add `service_account.json` to your project directory "
            "or configure `[gcp_service_account]` in `.streamlit/secrets.toml`."
        )

def clean_numeric_field(val):
    """
    Cleans field-entered relay numbers:
    - Fixes letter 'O' typed instead of '0' (e.g., 'O.03' -> 0.03, 'O' -> 0.0)
    - Strips '%' signs and whitespace
    - Returns None for 'No relay', blanks, or non-numeric text
    """
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    if not s or "no relay" in s.lower() or s.lower() in ("nan", "none", "na", "n/a", "-"):
        return None
    # Python 3.11+ requires (?i) at the very start of each regex pattern
    s = re.sub(r'(?i)\bO\b', '0', s)
    s = re.sub(r'(?i)O(?=\.)', '0', s)
    s = re.sub(r'(?i)(?<=\.)O', '0', s)
    s = s.replace("%", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None

def parse_ct_primary(ct_str):
    """Extracts primary CT amps from '200/5', '600/1', or '300'."""
    if not ct_str:
        return None
    s = str(ct_str).strip()
    if "/" in s:
        s = s.split("/")[0]
    return clean_numeric_field(s)

def normalize_voltage_label(v_str):
    """Maps '33 KV PTR protection', '11 KV', '132 KV' -> '33 kV', '11 kV', '132 kV'."""
    s = str(v_str).lower()
    if "132" in s:
        return "132 kV"
    elif "33" in s:
        return "33 kV"
    return "11 kV"

def normalize_curve_label(c_str):
    """Maps '1.3', '1.3Sec', '1.3 Sec' -> '1.3 Sec curve', and '3', '3.0', '3.0Sec' -> '3.0 Sec curve'."""
    s = str(c_str).strip().lower()
    if "1.3" in s or "1,3" in s:
        return "1.3 Sec curve"
    elif "3.0" in s or re.search(r"\b3\b", s):
        return "3.0 Sec curve"
    return "1.3 Sec curve"

@st.cache_data(ttl=300, show_spinner=False)
def fetch_google_sheet_tabs():
    """Fetches available worksheet names from the Google Sheet."""
    client = get_gspread_client()
    sh = client.open_by_url(GSHEET_URL)
    return [ws.title for ws in sh.worksheets()]

@st.cache_data(ttl=300, show_spinner=False)
def fetch_and_parse_sheet_tab(tab_name):
    """
    Reads either 'Form Responses 1' (coalescing ALL conditional section columns per row
    and converting CT % pickups/high-sets to Primary Amps) or a previously saved coordination tab.
    """
    client = get_gspread_client()
    sh = client.open_by_url(GSHEET_URL)
    ws = sh.worksheet(tab_name)
    all_values = ws.get_all_values()
    if not all_values or len(all_values) < 2:
        return enforce_float_dtypes(pd.DataFrame(columns=COLUMNS))

    headers = [str(h).strip() for h in all_values[0]]
    rows = all_values[1:]

    # Case A: Already in Coordination Database format (e.g. saved Division or Date tab)
    if "OC Pick-up (A)" in headers and "Substation" in headers:
        df_saved = pd.DataFrame(rows, columns=headers)
        for col in COLUMNS:
            if col not in df_saved.columns:
                df_saved[col] = np.nan
        df_saved["Division"] = df_saved["Division"].replace("", tab_name).fillna(tab_name)
        return enforce_float_dtypes(df_saved)

    # Case B: 'Form Responses 1' with conditional section columns across multiple branches
    def coalesce_row_by_keywords(r_padded, keywords, exclude=None):
        """
        Scans EVERY column in the row whose header matches `keywords` (not in `exclude`)
        and returns the first non-blank value, completely skipping empty conditional sections.
        """
        exclude = exclude or []
        for i, h in enumerate(headers):
            hl = h.lower()
            if any(ex in hl for ex in exclude):
                continue
            if any(k in hl for k in keywords):
                val = r_padded[i].strip()
                if val and val.lower() not in ("nan", "none", "null"):
                    return val
        return ""

    parsed_records = []

    for r in rows:
        r_padded = r + [""] * (len(headers) - len(r))

        div_val = coalesce_row_by_keywords(r_padded, ["division", "name of division", "div"])
        if not div_val:
            continue

        substation_raw = coalesce_row_by_keywords(
            r_padded, ["substation", "sub-station", "s/s", "33/11"], exclude=["feeder", "code"]
        )
        substation_val = substation_raw.upper() if substation_raw else "UNSPECIFIED"

        feeder_val = coalesce_row_by_keywords(
            r_padded, ["name of feeder", "feeder", "bay"], exclude=["voltage", "code"]
        )
        if not feeder_val:
            continue

        raw_voltage = coalesce_row_by_keywords(r_padded, ["feeder voltage", "voltage"])
        voltage_val = normalize_voltage_label(raw_voltage) if raw_voltage else "11 kV"

        raw_ct = coalesce_row_by_keywords(r_padded, ["ct rating", "ct ratio", "ct"])
        ct_pri = parse_ct_primary(raw_ct)

        raw_curve = coalesce_row_by_keywords(r_padded, ["idmt curve", "curve"])
        curve_val = normalize_curve_label(raw_curve)

        raw_oc_pct = coalesce_row_by_keywords(r_padded, ["oc pick", "idmt oc", "overcurrent pick"])
        raw_oc_tms = coalesce_row_by_keywords(r_padded, ["tms value of oc", "oc tms"], exclude=["ef", "earth", "51n", "51g"])
        raw_oc_hs_pct = coalesce_row_by_keywords(r_padded, ["over current high set", "oc high set", "oc inst"], exclude=["ef", "earth"])

        raw_ef_pct = coalesce_row_by_keywords(r_padded, ["earth fault percentage", "idmt earth fault", "ef pick"])
        raw_ef_tms = coalesce_row_by_keywords(r_padded, ["tms value of ef", "ef tms", "earth tms"], exclude=["oc", "overcurrent"])
        raw_ef_hs_pct = coalesce_row_by_keywords(r_padded, ["earth fault high set", "ef high set", "ef inst"], exclude=["oc", "overcurrent"])

        oc_pct = clean_numeric_field(raw_oc_pct)
        oc_tms = clean_numeric_field(raw_oc_tms)
        oc_hs_pct = clean_numeric_field(raw_oc_hs_pct)

        ef_pct = clean_numeric_field(raw_ef_pct)
        ef_tms = clean_numeric_field(raw_ef_tms)
        ef_hs_pct = clean_numeric_field(raw_ef_hs_pct)

        # Convert Percentage of CT Rating -> Primary Amps
        oc_pu_amps = round(ct_pri * (oc_pct / 100.0), 2) if (ct_pri and oc_pct and oc_pct > 0) else None
        oc_inst_amps = round(ct_pri * (oc_hs_pct / 100.0), 2) if (ct_pri and oc_hs_pct and oc_hs_pct > 0) else None
        ef_pu_amps = round(ct_pri * (ef_pct / 100.0), 2) if (ct_pri and ef_pct and ef_pct > 0) else None
        ef_inst_amps = round(ct_pri * (ef_hs_pct / 100.0), 2) if (ct_pri and ef_hs_pct and ef_hs_pct > 0) else None

        # Infer PTR MVA if mentioned in Feeder name (e.g., '33 KV 5 MVA PTR' -> 5.0)
        ptr_mva_inferred = None
        m_mva = re.search(r"(\d+(?:\.\d+)?)\s*MVA", feeder_val.upper())
        if m_mva:
            ptr_mva_inferred = float(m_mva.group(1))

        # Check if NGR resistance was recorded anywhere in the form
        raw_ngr = coalesce_row_by_keywords(r_padded, ["ngr", "neutral earth", "neutral resistance", "earthing resistance"])
        ngr_val = clean_numeric_field(raw_ngr)
        max_ngr_ef = None
        if ngr_val and ngr_val > 0:
            v_kv_num = parse_voltage(voltage_val)
            max_ngr_ef = round(((v_kv_num * 1000.0) / math.sqrt(3)) / ngr_val, 2)

        parsed_records.append({
            "Division": div_val,
            "Substation": substation_val,
            "Feeder": feeder_val,
            "Voltage": voltage_val,
            "CT (A)": ct_pri,
            "Curve": curve_val,
            "PTR MVA": ptr_mva_inferred,
            "PTR %Z": None,
            "PTR NGR (Ohms)": ngr_val,
            "Max NGR EF (A)": max_ngr_ef,
            "OC Pick-up (A)": oc_pu_amps,
            "OC TMS": float(oc_tms) if oc_tms is not None else np.nan,
            "OC Inst (A)": oc_inst_amps,
            "EF Pick-up (A)": ef_pu_amps,
            "EF TMS": float(ef_tms) if ef_tms is not None else np.nan,
            "EF Inst (A)": ef_inst_amps
        })

    return enforce_float_dtypes(pd.DataFrame(parsed_records, columns=COLUMNS))

def sanitize_tab_name(raw_name):
    if not raw_name or pd.isna(raw_name) or str(raw_name).strip() == "":
        return datetime.now().strftime("%d-%b-%Y")
    clean = re.sub(r'[\\/*?:\[\]]', '-', str(raw_name).strip().upper())
    return clean[:95]

def sync_to_gsheet(dataframe, mode="overwrite", division_tab=None):
    client = get_gspread_client()
    sh = client.open_by_url(GSHEET_URL)
    tab_name = sanitize_tab_name(division_tab)
    clean_df = dataframe.fillna("").astype(str)
    
    try:
        worksheet = sh.worksheet(tab_name)
        if mode == "overwrite":
            worksheet.clear()
            data_to_write = [clean_df.columns.values.tolist()] + clean_df.values.tolist()
            worksheet.update(values=data_to_write, range_name="A1")
        elif mode == "append":
            existing_records = worksheet.get_all_values()
            if not existing_records:
                worksheet.append_row(clean_df.columns.values.tolist())
            worksheet.append_rows(clean_df.values.tolist())
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=tab_name, rows="200", cols="25")
        data_to_write = [clean_df.columns.values.tolist()] + clean_df.values.tolist()
        worksheet.update(values=data_to_write, range_name="A1")
        
    return tab_name

def sync_analytics_to_gsheet(analytics_data):
    client = get_gspread_client()
    sh = client.open_by_url(GSHEET_URL)
    tab_name = "Analytics_" + datetime.now().strftime("%d-%b-%Y")
    
    headers = list(analytics_data.keys())
    values = [str(v) if pd.notna(v) and v is not None else "N/A" for v in analytics_data.values()]
    
    try:
        worksheet = sh.worksheet(tab_name)
        existing_records = worksheet.get_all_values()
        if not existing_records:
            worksheet.append_row(headers)
        worksheet.append_row(values)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=tab_name, rows="200", cols="25")
        worksheet.append_row(headers)
        worksheet.append_row(values)
        
    return tab_name

db_df = load_db()

# --- CORE MATH & PTR NGR LOGIC ---
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
        return float(str(v_str).lower().replace("kv", "").strip())
    except Exception:
        return 11.0

def calculate_ptr_ngr_and_sc(v_ll_kv, ptr_mva, ptr_z_pct, ngr_ohms, ct_primary):
    v_ln_volts = (v_ll_kv * 1000.0) / math.sqrt(3)
    
    i_fl_amps = None
    i_sc_max_3ph = None
    if ptr_mva and pd.notna(ptr_mva) and float(ptr_mva) > 0 and ptr_z_pct and pd.notna(ptr_z_pct) and float(ptr_z_pct) > 0:
        i_fl_amps = (float(ptr_mva) * 1e6) / (math.sqrt(3) * v_ll_kv * 1000.0)
        i_sc_max_3ph = i_fl_amps / (float(ptr_z_pct) / 100.0)

    i_ef_max = None
    rec_ef_pickup = (float(ct_primary) * 0.20) if (ct_primary and pd.notna(ct_primary)) else None
    plug_pct = 20.0

    if ngr_ohms and pd.notna(ngr_ohms) and float(ngr_ohms) > 0:
        i_ef_max = v_ln_volts / float(ngr_ohms)
        rec_ef_pickup = i_ef_max * 0.20
        if ct_primary and pd.notna(ct_primary) and float(ct_primary) > 0:
            plug_pct = (rec_ef_pickup / float(ct_primary)) * 100.0

    return {
        "v_ln_volts": round(v_ln_volts, 2),
        "i_fl_amps": round(i_fl_amps, 2) if i_fl_amps else None,
        "max_3ph_sc_A": round(i_sc_max_3ph, 2) if i_sc_max_3ph else None,
        "max_ef_current_A": round(i_ef_max, 2) if i_ef_max else None,
        "rec_ef_pickup_A": round(rec_ef_pickup, 2) if rec_ef_pickup else None,
        "plug_setting_pct": round(plug_pct, 2)
    }

def calculate_high_set_limit(up_row, down_row, fault_type="OC", margin=0.100, max_fc=50000):
    down_v = parse_voltage(down_row["Voltage"])
    up_v = parse_voltage(up_row["Voltage"])
    trans_ratio = down_v / up_v 
    
    c_down = CURVE_CONSTANTS.get(down_row["Curve"], 0.0607)
    c_up = CURVE_CONSTANTS.get(up_row["Curve"], 0.0607)
    
    down_pu = pd.to_numeric(down_row.get(f"{fault_type} Pick-up (A)"), errors="coerce")
    down_tms = pd.to_numeric(down_row.get(f"{fault_type} TMS"), errors="coerce")
    up_pu = pd.to_numeric(up_row.get(f"{fault_type} Pick-up (A)"), errors="coerce")
    up_tms = pd.to_numeric(up_row.get(f"{fault_type} TMS"), errors="coerce")

    if pd.isna(down_pu) or pd.isna(up_pu) or pd.isna(down_tms) or pd.isna(up_tms):
        return None, None, None, None, trans_ratio, "Pick-up or TMS parameters missing."

    up_ptr_stats = calculate_ptr_ngr_and_sc(
        up_v, up_row.get("PTR MVA"), up_row.get("PTR %Z"), up_row.get("PTR NGR (Ohms)"), up_row.get("CT (A)")
    )
    down_ptr_stats = calculate_ptr_ngr_and_sc(
        down_v, down_row.get("PTR MVA"), down_row.get("PTR %Z"), down_row.get("PTR NGR (Ohms)"), down_row.get("CT (A)")
    )

    physical_ceiling = None
    if fault_type == "EF":
        ngr_ceilings = []
        if down_ptr_stats["max_ef_current_A"]:
            ngr_ceilings.append(down_ptr_stats["max_ef_current_A"])
        if up_ptr_stats["max_ef_current_A"]:
            ngr_ceilings.append(up_ptr_stats["max_ef_current_A"] / trans_ratio)
        if ngr_ceilings:
            physical_ceiling = min(ngr_ceilings)
    elif fault_type == "OC":
        sc_ceilings = []
        if down_ptr_stats["max_3ph_sc_A"]:
            sc_ceilings.append(down_ptr_stats["max_3ph_sc_A"])
        if up_ptr_stats["max_3ph_sc_A"]:
            sc_ceilings.append(up_ptr_stats["max_3ph_sc_A"] / trans_ratio)
        if sc_ceilings:
            physical_ceiling = min(sc_ceilings)

    search_upper = min(float(max_fc), physical_ceiling) if physical_ceiling else float(max_fc)

    def get_margin(I_down):
        I_up = I_down * trans_ratio
        t_d = calc_time(I_down, down_pu, down_tms, c_down)
        t_u = calc_time(I_up, up_pu, up_tms, c_up)
        if np.isnan(t_d) or np.isnan(t_u):
            return np.nan
        return t_u - t_d

    min_i_down = down_pu * 1.05
    min_i_up_referred = (up_pu / trans_ratio) * 1.05
    low = max(min_i_down, min_i_up_referred)
    high = search_upper
    math_limit = None
    msg = ""

    if low >= high:
        math_limit = high
        msg = f"Bounded directly by physical fault ceiling ({high:.0f} A)."
    else:
        m_low = get_margin(low)
        m_high = get_margin(high)

        if np.isnan(m_low):
            msg = "Relays do not operate at low currents."
        elif m_low < margin:
            msg = f"Grading Fails early (margin {m_low*1000:.0f}ms at pick-up)."
        elif m_high > margin:
            math_limit = high
            msg = f"Selective across full fault range up to {high:.0f} A."
        else:
            mid = low
            for _ in range(100):
                mid = (low + high) / 2.0
                m_mid = get_margin(mid)
                if np.isnan(m_mid):
                    break
                if m_mid > margin:
                    low = mid
                else:
                    high = mid
                if abs(m_mid - margin) < 0.001:
                    break
            math_limit = mid
            msg = f"Mathematical convergence at {math_limit:.0f} A."

    upstream_inst = pd.to_numeric(up_row.get(f"{fault_type} Inst (A)"), errors="coerce")
    ceiling_limit = None
    if pd.notna(upstream_inst) and float(upstream_inst) > 0:
        ceiling_limit = float(upstream_inst) / trans_ratio

    final_limit = math_limit
    if ceiling_limit is not None and (final_limit is None or ceiling_limit < final_limit):
        final_limit = ceiling_limit
        msg = "Capped by Upstream High-Set limit."
    if physical_ceiling is not None and (final_limit is None or physical_ceiling < final_limit):
        final_limit = physical_ceiling
        msg = f"Capped by PTR {'NGR Earth Fault' if fault_type == 'EF' else '%Z Short-Circuit'} limit ({physical_ceiling:.0f} A)."

    return final_limit, math_limit, ceiling_limit, physical_ceiling, trans_ratio, msg

# --- UI INJECTION ---
st.sidebar.header("🎨 UI Settings")
dark_mode = st.sidebar.toggle("🌙 Enable Dark Mode", value=True)
if dark_mode:
    st.markdown("""<style>.stApp { background-color: #0E1117; color: #FAFAFA; }</style>""", unsafe_allow_html=True)
    plot_template = "plotly_dark"
else:
    plot_template = "plotly_white"

sync_gsheet_auto = st.sidebar.toggle("☁️ Auto-Sync to Google Sheets", value=True)

# -------------------------------------------------------------------------
# FETCH & IMPORT FROM GOOGLE SHEET ('Form Responses 1' & SAVED TABS)
# -------------------------------------------------------------------------
with st.expander("📥 Fetch Relay Settings from Google Sheet ('Form Responses 1' & Saved Tabs)", expanded=True):
    try:
        available_tabs = fetch_google_sheet_tabs()
        default_tab_idx = available_tabs.index("Form Responses 1") if "Form Responses 1" in available_tabs else 0
        
        imp_col1, imp_col2, imp_col3, imp_col4 = st.columns([1.2, 1.2, 1.3, 1.3])
        with imp_col1:
            selected_tab = st.selectbox("1️⃣ Select Sheet Tab", available_tabs, index=default_tab_idx)
            if st.button("🔄 Refresh Sheet Data"):
                st.cache_data.clear()
                st.rerun()

        df_sheet_raw = fetch_and_parse_sheet_tab(selected_tab)

        if not df_sheet_raw.empty:
            filled_divisions = sorted([
                d for d in df_sheet_raw["Division"].dropna().unique().tolist()
                if str(d).strip() != ""
            ])
            with imp_col2:
                sel_div_import = st.selectbox("2️⃣ Select Division (Populated Only)", filled_divisions, index=0)

            df_div_filtered = df_sheet_raw[df_sheet_raw["Division"] == sel_div_import]
            filled_substations = sorted([
                s for s in df_div_filtered["Substation"].dropna().unique().tolist()
                if str(s).strip() != ""
            ])
            with imp_col3:
                sel_ss_import = st.selectbox(
                    "3️⃣ Select Substation",
                    ["ALL SUBSTATIONS"] + filled_substations,
                    index=0
                )

            if sel_ss_import != "ALL SUBSTATIONS":
                df_to_import = df_div_filtered[df_div_filtered["Substation"] == sel_ss_import].copy()
            else:
                df_to_import = df_div_filtered.copy()

            with imp_col4:
                st.metric("Matching Relay Rows", f"{len(df_to_import)} Feeders", f"Division: {sel_div_import}")
                b_rep, b_app = st.columns(2)
                if b_rep.button("⬇️ Load (Replace Table)", type="primary", use_container_width=True):
                    save_db(df_to_import)
                    st.success(f"Loaded {len(df_to_import)} rows for {sel_div_import}!")
                    st.rerun()
                if b_app.button("➕ Append to Table", use_container_width=True):
                    combined = pd.concat([db_df, df_to_import], ignore_index=True)
                    save_db(combined)
                    st.success(f"Appended {len(df_to_import)} rows!")
                    st.rerun()
        else:
            st.info(f"Tab `{selected_tab}` has no populated relay rows.")
    except Exception as e:
        st.warning(f"⚠️ Could not connect to Google Sheet to fetch data: {e}")

# --- SIDEBAR MANUAL INPUTS ---
st.sidebar.header("1. Feeder & PTR Details")
division = st.sidebar.text_input("Division Name (Target Sheet Tab)", placeholder="e.g. Chaurai", value="Chaurai")
substation = st.sidebar.text_input("Substation Name", placeholder="e.g. BELKHEDA", value=None)
feeder = st.sidebar.text_input("Feeder Name", placeholder="e.g. 11kv main", value=None)
voltage_level = st.sidebar.selectbox("Voltage Level", ["132 kV", "33 kV", "11 kV"], index=2)
ct_rating = st.sidebar.number_input("CT Rating (Primary Amps)", min_value=10.0, step=50.0, value=None)
curve_type = st.sidebar.selectbox("Curve Type", list(CURVE_CONSTANTS.keys()), index=0)

st.sidebar.header("2. PTR Impedance & Neutral Earth Resistance")
ptr_mva = st.sidebar.number_input("PTR Capacity (MVA) [Optional]", min_value=0.5, step=0.5, value=None)
ptr_z_pct = st.sidebar.number_input("PTR Impedance (%Z) [Optional]", min_value=1.0, step=0.05, value=None)
ptr_ngr_ohms = st.sidebar.number_input("PTR Neutral Earth Resistance (Ohms) [Optional]", min_value=0.0, step=1.0, value=None)

v_num_sidebar = parse_voltage(voltage_level)
ptr_preview = calculate_ptr_ngr_and_sc(v_num_sidebar, ptr_mva, ptr_z_pct, ptr_ngr_ohms, ct_rating)
if ptr_preview["max_ef_current_A"] or ptr_preview["max_3ph_sc_A"]:
    preview_lines = []
    if ptr_preview["max_3ph_sc_A"]:
        preview_lines.append(f"Max 3Ph Fault (%Z): **{ptr_preview['max_3ph_sc_A']} A**")
    if ptr_preview["max_ef_current_A"]:
        preview_lines.append(
            f"Max NGR EF Current: **{ptr_preview['max_ef_current_A']} A**  \n"
            f"Rec. 51G/EF Pickup (20%): **{ptr_preview['rec_ef_pickup_A']} A** ({ptr_preview['plug_setting_pct']}% CT)"
        )
    st.sidebar.info("\n\n".join(preview_lines))

st.sidebar.header("3. Phase Overcurrent (OC)")
oc_pickup = st.sidebar.number_input("OC Pick-up (A)", min_value=1.0, step=5.0, value=None)
oc_tms = st.sidebar.number_input("OC TMS", min_value=0.005, max_value=5.0, step=0.005, format="%.4f", value=None)
oc_inst = st.sidebar.number_input("OC Inst / High-Set (A) [Optional]", min_value=10.0, step=10.0, value=None)

st.sidebar.header("4. Earth Fault (EF)")
ef_default_val = float(ptr_preview["rec_ef_pickup_A"]) if (ptr_ngr_ohms and ptr_preview["rec_ef_pickup_A"]) else None
ef_pickup = st.sidebar.number_input("EF Pick-up (A)", min_value=1.0, step=1.0, value=ef_default_val)
ef_tms = st.sidebar.number_input("EF TMS", min_value=0.005, max_value=5.0, step=0.005, format="%.4f", value=None)
ef_inst = st.sidebar.number_input("EF Inst / High-Set (A) [Optional]", min_value=10.0, step=10.0, value=None)

if st.sidebar.button("💾 Add Relay to Database", type="primary"):
    if None in [substation, feeder, ct_rating] or substation == "" or feeder == "":
        st.sidebar.error("⚠️ Please fill out Substation, Feeder, and CT Rating.")
    else:
        new_row = enforce_float_dtypes(pd.DataFrame([{
            "Division": division.strip() if division else "General",
            "Substation": substation.strip(),
            "Feeder": feeder.strip(),
            "Voltage": voltage_level,
            "CT (A)": float(ct_rating),
            "Curve": curve_type,
            "PTR MVA": float(ptr_mva) if ptr_mva is not None else np.nan,
            "PTR %Z": float(ptr_z_pct) if ptr_z_pct is not None else np.nan,
            "PTR NGR (Ohms)": float(ptr_ngr_ohms) if ptr_ngr_ohms is not None else np.nan,
            "Max NGR EF (A)": float(ptr_preview["max_ef_current_A"]) if ptr_preview["max_ef_current_A"] is not None else np.nan,
            "OC Pick-up (A)": float(oc_pickup) if oc_pickup is not None else np.nan,
            "OC TMS": round(float(oc_tms), 4) if oc_tms is not None else np.nan,
            "OC Inst (A)": float(oc_inst) if oc_inst is not None else np.nan,
            "EF Pick-up (A)": float(ef_pickup) if ef_pickup is not None else np.nan,
            "EF TMS": round(float(ef_tms), 4) if ef_tms is not None else np.nan,
            "EF Inst (A)": float(ef_inst) if ef_inst is not None else np.nan
        }]))
        updated_db = pd.concat([db_df, new_row], ignore_index=True)
        save_db(updated_db)
        st.sidebar.success("Added to local database!")
        
        if sync_gsheet_auto:
            try:
                tab_created = sync_to_gsheet(new_row, mode="append", division_tab=division)
                st.sidebar.success(f"☁️ Appended to Google Sheet tab: **{tab_created}**")
            except Exception as e:
                st.sidebar.error(f"Google Sheet Sync Failed: {e}")
        st.rerun()

st.divider()

# --- DATABASE TABLE ---
col_table, col_calc = st.columns([2.2, 1])

with col_table:
    st.subheader("Coordination Database")
    if not db_df.empty:
        db_df = enforce_float_dtypes(db_df)
        edited_df = st.data_editor(
            db_df,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "CT (A)": st.column_config.NumberColumn("CT (A)", format="%.1f", step=10.0),
                "PTR MVA": st.column_config.NumberColumn("PTR MVA", format="%.2f", step=0.5),
                "PTR %Z": st.column_config.NumberColumn("PTR %Z", format="%.2f", step=0.05),
                "PTR NGR (Ohms)": st.column_config.NumberColumn("PTR NGR (Ohms)", format="%.1f", step=1.0),
                "Max NGR EF (A)": st.column_config.NumberColumn("Max NGR EF (A)", format="%.2f", disabled=True),
                "OC Pick-up (A)": st.column_config.NumberColumn("OC Pick-up (A)", format="%.2f", step=1.0),
                "OC TMS": st.column_config.NumberColumn("OC TMS", min_value=0.005, max_value=5.0, step=0.005, format="%.4f"),
                "OC Inst (A)": st.column_config.NumberColumn("OC Inst (A)", format="%.1f", step=10.0),
                "EF Pick-up (A)": st.column_config.NumberColumn("EF Pick-up (A)", format="%.2f", step=1.0),
                "EF TMS": st.column_config.NumberColumn("EF TMS", min_value=0.005, max_value=5.0, step=0.005, format="%.4f"),
                "EF Inst (A)": st.column_config.NumberColumn("EF Inst (A)", format="%.1f", step=10.0),
            }
        )
        
        btn_col1, btn_col2, btn_col3 = st.columns(3)
        if btn_col1.button("🔄 Commit Table Changes", use_container_width=True):
            edited_df = enforce_float_dtypes(edited_df)
            for idx, r in edited_df.iterrows():
                stats = calculate_ptr_ngr_and_sc(
                    parse_voltage(r.get("Voltage")), r.get("PTR MVA"), r.get("PTR %Z"), r.get("PTR NGR (Ohms)"), r.get("CT (A)")
                )
                edited_df.at[idx, "Max NGR EF (A)"] = stats["max_ef_current_A"]

            save_db(edited_df)
            st.success("Local CSV database updated!")
            if sync_gsheet_auto:
                try:
                    synced_tabs = []
                    for div_name, grp in edited_df.groupby(edited_df["Division"].fillna("General")):
                        t_name = sync_to_gsheet(grp, mode="overwrite", division_tab=div_name)
                        synced_tabs.append(t_name)
                    st.success(f"☁️ Synced to Division tab(s): **{', '.join(synced_tabs)}**")
                except Exception as e:
                    st.error(f"Google Sheet Sync Failed: {e}")
            st.rerun()
            
        if btn_col2.button("📤 Save to Google Sheet (Division Tabs)", type="secondary", use_container_width=True):
            try:
                edited_df = enforce_float_dtypes(edited_df)
                with st.spinner("Uploading to Google Sheets by Division tab..."):
                    synced_tabs = []
                    for div_name, grp in edited_df.groupby(edited_df["Division"].fillna("General")):
                        t_name = sync_to_gsheet(grp, mode="overwrite", division_tab=div_name)
                        synced_tabs.append(t_name)
                st.success(f"✅ Saved to Division tab(s): **{', '.join(synced_tabs)}**!")
            except Exception as e:
                st.error(f"❌ Failed to save to Google Sheet: {e}")

        if btn_col3.button("🗑️ Clear Table", use_container_width=True):
            save_db(pd.DataFrame(columns=COLUMNS))
            st.rerun()
    else:
        st.info("The database is empty. Fetch a Division/Substation from `Form Responses 1` above or add a relay in the sidebar.")

# --- MARGIN ANALYTICS & HIGH-SET CALCULATOR ---
with col_calc:
    st.subheader("🔍 Margin Analytics")
    st.markdown("Calculate safe High-Set constraints based on Math Limits, Upstream Ceilings & PTR NGR.")
    
    if not db_df.empty and len(db_df) >= 2:
        relay_options = [f"{row['Substation']} - {row['Feeder']} ({row['Voltage']})" for _, row in db_df.iterrows()]
        
        up_selection = st.selectbox("Select Upstream Relay", relay_options, index=0)
        down_selection = st.selectbox("Select Downstream Relay", relay_options, index=1)
        
        if st.button("Calculate High-Set Limits", type="primary"):
            up_idx = relay_options.index(up_selection)
            down_idx = relay_options.index(down_selection)
            
            up_row = db_df.iloc[up_idx]
            down_row = db_df.iloc[down_idx]
            
            limit_oc, math_oc, ceil_oc, phys_oc, t_ratio, msg_oc = calculate_high_set_limit(up_row, down_row, fault_type="OC")
            limit_ef, math_ef, ceil_ef, phys_ef, _, msg_ef = calculate_high_set_limit(up_row, down_row, fault_type="EF")
            
            if t_ratio != 1.0:
                st.info(f"🔄 **Transformer Reflection:** Downstream faults are multiplied by **{t_ratio:.3f}**.")
            elif up_row['Voltage'] == down_row['Voltage']:
                st.info("⚡ **Same Voltage Bus:** No transformer reflection applied.")

            st.markdown("---")
            st.markdown("### Phase Overcurrent (OC)")
            if math_oc: st.write(f"📉 **Math Limit:** {math_oc:.0f} A")
            if ceil_oc: st.write(f"🛑 **Upstream Ceiling:** {ceil_oc:.0f} A")
            if phys_oc: st.write(f"🏭 **PTR %Z 3Ph Fault Ceiling:** {phys_oc:.0f} A")
            
            if limit_oc:
                st.error(f"**Required Setting:** Set OC High-Set <= **{limit_oc:.0f} A**")
                st.caption(msg_oc)
                st.session_state['hs_limit_oc'] = limit_oc
            else:
                st.warning(msg_oc)
                
            st.markdown("---")
            st.markdown("### Earth Fault (EF)")
            if math_ef: st.write(f"📉 **Math Limit:** {math_ef:.0f} A")
            if ceil_ef: st.write(f"🛑 **Upstream Ceiling:** {ceil_ef:.0f} A")
            if phys_ef: st.write(f"🌍 **PTR NGR Max EF Ceiling:** {phys_ef:.0f} A")
            
            if limit_ef:
                if limit_oc and limit_ef > limit_oc:
                    practical_limit_ef = limit_oc
                    st.error(f"**Required Setting:** Set EF High-Set <= **{practical_limit_ef:.0f} A**")
                    st.caption("*(EF constrained mathematically/NGR, but further capped by practical OC limit).*")
                    st.session_state['hs_limit_ef'] = practical_limit_ef
                    limit_ef = practical_limit_ef
                else:
                    st.error(f"**Required Setting:** Set EF High-Set <= **{limit_ef:.0f} A**")
                    st.caption(msg_ef)
                    st.session_state['hs_limit_ef'] = limit_ef
            else:
                st.warning(msg_ef)
                
            st.session_state['hs_down_v'] = parse_voltage(down_row["Voltage"])
            
            st.session_state['last_analytics'] = {
                "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Division": up_row.get("Division", ""),
                "Upstream Relay": up_selection,
                "Downstream Relay": down_selection,
                "PTR NGR (Ohms)": up_row.get("PTR NGR (Ohms)", ""),
                "Max NGR EF Ceiling (A)": round(phys_ef, 2) if phys_ef else None,
                "OC Math Limit (A)": round(math_oc, 2) if math_oc else None,
                "OC Ceiling Limit (A)": round(ceil_oc, 2) if ceil_oc else None,
                "OC Recommended Limit (A)": round(limit_oc, 2) if limit_oc else None,
                "OC Message": msg_oc,
                "EF Math Limit (A)": round(math_ef, 2) if math_ef else None,
                "EF Ceiling Limit (A)": round(ceil_ef, 2) if ceil_ef else None,
                "EF Recommended Limit (A)": round(limit_ef, 2) if limit_ef else None,
                "EF Message": msg_ef
            }

        if 'last_analytics' in st.session_state:
            st.markdown("---")
            if st.button("📤 Export Analytics to Sheets"):
                try:
                    with st.spinner("Saving analytics to Google Sheets..."):
                        analytics_tab = sync_analytics_to_gsheet(st.session_state['last_analytics'])
                    st.success(f"✅ Saved calculations to **{analytics_tab}**!")
                except Exception as e:
                    st.error(f"❌ Failed to save analytics: {e}")
            
    else:
        st.warning("Load or save at least two relays to the database to use this feature.")

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
        
        pu_val = pd.to_numeric(row.get(f"{fault_type_key} Pick-up (A)"), errors="coerce")
        tms_val = pd.to_numeric(row.get(f"{fault_type_key} TMS"), errors="coerce")
        inst_val = pd.to_numeric(row.get(f"{fault_type_key} Inst (A)"), errors="coerce")
        
        if pd.isna(pu_val) or pd.isna(tms_val):
            continue 
        
        pu_val = float(pu_val)
        tms_val = float(tms_val)
        
        row_ptr_stats = calculate_ptr_ngr_and_sc(
            row_v_val, row.get("PTR MVA"), row.get("PTR %Z"), row.get("PTR NGR (Ohms)"), row.get("CT (A)")
        )
        max_phys_base = None
        if fault_type_key == "EF" and row_ptr_stats["max_ef_current_A"]:
            max_phys_base = row_ptr_stats["max_ef_current_A"] * (row_v_val / base_v_val)
        elif fault_type_key == "OC" and row_ptr_stats["max_3ph_sc_A"]:
            max_phys_base = row_ptr_stats["max_3ph_sc_A"] * (row_v_val / base_v_val)

        x_vals = []
        y_vals = []
        
        inst_val_base = float(inst_val) * (row_v_val / base_v_val) if pd.notna(inst_val) and float(inst_val) > 0 else None

        for fc_base in base_plot_currents:
            if max_phys_base and fc_base > max_phys_base:
                continue
            if inst_val_base and fc_base > inst_val_base:
                continue 
                
            fc_relay = fc_base * (base_v_val / row_v_val)
            t_idmt = calc_time(fc_relay, pu_val, tms_val, c_const)
            
            if not np.isnan(t_idmt):
                x_vals.append(fc_base)
                y_vals.append(t_idmt)

        upper_plot_bound = max_phys_base if max_phys_base else max(base_plot_currents)
        if inst_val_base and inst_val_base <= upper_plot_bound:
            t_idmt_at_inst = calc_time(inst_val_base * (base_v_val / row_v_val), pu_val, tms_val, c_const)
            if not np.isnan(t_idmt_at_inst):
                x_vals.append(inst_val_base)
                y_vals.append(t_idmt_at_inst) 
                x_vals.append(inst_val_base)
                y_vals.append(0.03) 
                x_vals.append(upper_plot_bound)
                y_vals.append(0.03)

        if fault_type_key == "EF" and max_phys_base:
            fig.add_vline(
                x=max_phys_base,
                line_dash="dot",
                line_color="purple",
                annotation_text=f" {row['Feeder']} NGR Max: {max_phys_base:.0f}A",
                annotation_position="bottom right"
            )

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
