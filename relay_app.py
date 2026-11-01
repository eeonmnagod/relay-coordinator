import io
import re
import math
import urllib.parse
from datetime import datetime
import requests
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

# ---------------------------------------------------------
# 1. GOOGLE SHEET BACKEND CONFIG & LIVE PARSER ENGINE
# ---------------------------------------------------------
DEFAULT_PEAK_LOAD_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1Cr3bHdLWR53skYROA4ZgGMScCjm0QHMAuvHKdIrnLvk/edit?usp=drivesdk"
)

KNOWN_MPEZ_CIRCLE_TABS = [
    "CHHINDWARA", "SEONI", "BALAGHAT", "NARSINGHPUR", "JABALPUR CITY",
    "JABALPUR (O&M)", "KATNI", "MANDLA", "DINDORI", "REWA", "SATNA",
    "SIDHI", "SINGROLI", "SHAHDOL", "UMARIYA", "ANUPPUR", "SAGAR",
    "DAMOH", "CHHATARPUR", "TIKAMGARH", "PANNA", "wp 2025-26"
]

CURVE_CONSTANTS = {
    "3.0 Sec curve (IEC Standard Inverse)": 0.14,
    "1.3 Sec curve (Fast Inverse)": 0.0607,
}

CONDUCTOR_PRESETS = {
    "ACSR Dog (100 sq.mm)": {"R1": 0.273, "X1": 0.335},
    "ACSR Raccoon (80 sq.mm)": {"R1": 0.366, "X1": 0.345},
    "ACSR Rabbit (50 sq.mm)": {"R1": 0.543, "X1": 0.359},
    "ACSR Weasel (30 sq.mm)": {"R1": 0.908, "X1": 0.380},
    "11kV XLPE Cable (3x300 sq.mm Al)": {"R1": 0.125, "X1": 0.088},
    "Custom R + jX": {"R1": 0.366, "X1": 0.345},
}

CT_CLASS_PRESETS = {
    "5P10": 10.0, "5P15": 15.0, "5P20": 20.0,
    "10P10": 10.0, "10P15": 15.0, "10P20": 20.0,
}

FEEDER_COLORS = ["#d32f2f", "#f57c00", "#7b1fa2", "#00796b", "#c2185b", "#5d4037", "#455a64", "#fbc02d"]


def extract_sheet_id(sheet_url_or_id):
    """Extracts the spreadsheet ID from a full Google Sheets URL."""
    m = re.search(r"/d/([a-zA-Z0-9-_]+)", str(sheet_url_or_id))
    return m.group(1) if m else str(sheet_url_or_id).strip()


def parse_ptr_mva_value(raw_val):
    """
    Extracts numerical PTR MVA capacity from strings such as:
    '8 MVA', '3.15 MVA (OLD)', '5 to 10 (New)', '2x3.15', '5.0', etc.
    """
    if pd.isna(raw_val):
        return 5.0
    s = str(raw_val).upper().strip()
    # Check "X to Y" augmentation pattern -> take upgraded capacity Y
    m_to = re.search(r"(\d+(?:\.\d+)?)\s*TO\s*(\d+(?:\.\d+)?)", s)
    if m_to:
        return float(m_to.group(2))
    # Standard numerical MVA value
    m_num = re.findall(r"\d+(?:\.\d+)?", s)
    if m_num:
        for val_str in reversed(m_num):
            v = float(val_str)
            if v in (1.6, 2.5, 3.15, 5.0, 6.3, 8.0, 10.0, 12.5, 16.0, 20.0):
                return v
        v0 = float(m_num[0])
        if 1.0 <= v0 <= 50.0:
            return v0
    return 5.0


def recommend_standard_ct(load_or_fl_amps):
    """Selects standard protection CT primary rating (A) above full load current."""
    standard_cts = [100, 150, 200, 300, 400, 600, 800, 1000, 1200]
    for ct in standard_cts:
        if ct >= load_or_fl_amps * 1.10:
            return float(ct)
    return 600.0


def normalize_sheet_dataframe(df_raw, fallback_circle="UNKNOWN"):
    """
    Auto-detects the header row in a Circle sheet tab and normalizes columns into:
    circle, division, feeder_33kv, feeder_33kv_code, substation, ss_code,
    ptr_mva_raw, ptr_mva, feeder_11kv, feeder_11kv_code, peak_load_amps
    """
    if df_raw is None or df_raw.empty:
        return pd.DataFrame()

    # Locate the row containing column headers (searching first 8 rows)
    header_idx = 0
    for idx in range(min(8, len(df_raw))):
        row_text = " ".join(str(x).lower() for x in df_raw.iloc[idx].values if pd.notna(x))
        if any(k in row_text for k in ["substation", "s/s", "division", "feeder", "mva", "ptr", "district", "ss code"]):
            header_idx = idx
            break

    df = df_raw.iloc[header_idx + 1:].copy()
    raw_cols = [str(c).strip() for c in df_raw.iloc[header_idx].values]
    # Deduplicate column names
    seen = {}
    clean_cols = []
    for c in raw_cols:
        c_key = c if c else "col"
        seen[c_key] = seen.get(c_key, 0) + 1
        clean_cols.append(f"{c_key}_{seen[c_key]}" if seen[c_key] > 1 else c_key)
    df.columns = clean_cols
    df = df.dropna(how="all")

    def find_col(keywords, exclude=None):
        exclude = exclude or []
        for col in df.columns:
            cl = col.lower()
            if any(k in cl for k in keywords) and not any(ex in cl for ex in exclude):
                return col
        return None

    col_circle = find_col(["circle", "district"])
    col_div = find_col(["division", "div"])
    col_33fdr = find_col(["33kv feeder", "33 kv feeder", "33kv fdr", "33 kv fdr", "source 33"], exclude=["code"])
    col_33code = find_col(["33kv code", "33 kv feeder code", "33kv fdr code"])
    col_ss = find_col(["33/11", "s/s", "substation", "sub-station"], exclude=["code"])
    col_sscode = find_col(["ss code", "s/s code", "substation code"])
    col_ptr = find_col(["ptr", "capacity", "mva", "work proposed", "transformer"])
    col_11fdr = find_col(["11kv feeder", "11 kv feeder", "11kv fdr", "outgoing feeder", "feeder name"], exclude=["33", "code"])
    col_11code = find_col(["11kv code", "11 kv feeder code", "feeder code", "fdr code"], exclude=["33"])
    col_load = find_col(["peak", "load", "amp", "max current", "current (a)"])

    records = []
    for _, r in df.iterrows():
        ss_val = str(r[col_ss]).strip() if col_ss and pd.notna(r[col_ss]) else ""
        if not ss_val or ss_val.lower() in ("nan", "none", "total", "s.no", "name of associated 33/11kv s/s"):
            continue

        circle_val = str(r[col_circle]).strip().upper() if col_circle and pd.notna(r[col_circle]) else fallback_circle.upper()
        div_val = str(r[col_div]).strip().upper() if col_div and pd.notna(r[col_div]) else f"{circle_val} DIVISION"
        f33_val = str(r[col_33fdr]).strip() if col_33fdr and pd.notna(r[col_33fdr]) else f"33kV {ss_val} Feeder"
        f33_code = str(r[col_33code]).strip() if col_33code and pd.notna(r[col_33code]) else f"33F-{ss_val[:4].upper()}"
        sscode_val = str(r[col_sscode]).strip().split(".")[0] if col_sscode and pd.notna(r[col_sscode]) else "N/A"
        ptr_raw = str(r[col_ptr]).strip() if col_ptr and pd.notna(r[col_ptr]) else "5.0 MVA"
        ptr_mva = parse_ptr_mva_value(ptr_raw)

        f11_val = str(r[col_11fdr]).strip() if col_11fdr and pd.notna(r[col_11fdr]) else ""
        f11_code = str(r[col_11code]).strip() if col_11code and pd.notna(r[col_11code]) else ""

        load_amps = 110.0
        if col_load and pd.notna(r[col_load]):
            m_ld = re.findall(r"\d+(?:\.\d+)?", str(r[col_load]))
            if m_ld:
                load_amps = float(m_ld[0])

        records.append({
            "circle": circle_val,
            "division": div_val,
            "feeder_33kv": f33_val,
            "feeder_33kv_code": f33_code,
            "substation": ss_val.upper(),
            "ss_code": sscode_val,
            "ptr_mva_raw": ptr_raw,
            "ptr_mva": ptr_mva,
            "feeder_11kv": f11_val,
            "feeder_11kv_code": f11_code,
            "peak_load_amps": load_amps
        })

    return pd.DataFrame(records)


@st.cache_data(ttl=600, show_spinner=False)
def load_company_peak_load_database(sheet_url):
    """
    Loads all Circle tabs from the Google Sheet workbook (via XLSX export or per-tab gviz CSV).
    Falls back to embedded MPPKVVCL data if the sheet is private and no file is uploaded.
    """
    sheet_id = extract_sheet_id(sheet_url)
    all_dfs = []
    loaded_tabs = []

    # 1. Try downloading complete multi-tab workbook via export?format=xlsx
    xlsx_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    try:
        resp = requests.get(xlsx_url, timeout=12)
        if resp.status_code == 200 and resp.content[:2] == b"PK":
            xls = pd.ExcelFile(io.BytesIO(resp.content))
            for tab_name in xls.sheet_names:
                df_tab_raw = pd.read_excel(xls, sheet_name=tab_name, header=None)
                df_norm = normalize_sheet_dataframe(df_tab_raw, fallback_circle=tab_name)
                if not df_norm.empty:
                    # If tab is a specific Circle name (not 'wp 2025-26'), tag circle with tab_name
                    if "wp" not in tab_name.lower() and "sheet" not in tab_name.lower():
                        df_norm["circle"] = tab_name.strip().upper()
                    all_dfs.append(df_norm)
                    loaded_tabs.append(tab_name)
    except Exception:
        pass

    # 2. If XLSX export didn't return tabs, query known Circle tabs via gviz CSV
    if not all_dfs:
        for tab in KNOWN_MPEZ_CIRCLE_TABS:
            encoded_tab = urllib.parse.quote(tab)
            csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={encoded_tab}"
            try:
                r_csv = requests.get(csv_url, timeout=5)
                if r_csv.status_code == 200 and "html" not in r_csv.text[:100].lower():
                    df_raw = pd.read_csv(io.StringIO(r_csv.text), header=None)
                    df_norm = normalize_sheet_dataframe(df_raw, fallback_circle=tab)
                    if not df_norm.empty:
                        all_dfs.append(df_norm)
                        loaded_tabs.append(tab)
            except Exception:
                continue

    if all_dfs:
        master_df = pd.concat(all_dfs, ignore_index=True).drop_duplicates()
        return master_df, loaded_tabs, "LIVE_GSHEET"

    # 3. Embedded Snapshot from Peak Loading FY 2026-27 (ensures zero-downtime operation)
    embedded_rows = [
        ("CHHINDWARA", "CHHINDWARA CITY", "33kV Pachmadhi Feeder", "33F-5155", "PACHMADHIIDHANA", "5155", "3.15 MVA (OLD)", 3.15),
        ("CHHINDWARA", "CHHINDWARA EAST", "33kV Sarora Feeder", "33F-5179", "SARORA", "5179", "3.15 to 5 (OLD)", 5.0),
        ("CHHINDWARA", "PANDHURNA", "33kV Rajana Feeder", "33F-5206", "RAJANA", "5206", "3.15 to 5 (OLD)", 5.0),
        ("CHHINDWARA", "PANDHURNA", "33kV Mordongri Feeder", "33F-5793", "MORDONGRI", "5793", "3.15 MVA (OLD)", 3.15),
        ("CHHINDWARA", "PANDHURNA", "33kV Teegaon Feeder", "33F-5207", "TEEGAON", "5207", "5 to 10 (New)", 10.0),
        ("CHHINDWARA", "PANDHURNA", "33kV Chimankhapa Feeder", "33F-6591", "CHIMANKHAPA", "6591", "3.15 MVA (OLD)", 3.15),
        ("CHHINDWARA", "PANDHURNA", "33kV Jatlapur Feeder", "33F-7034", "JATLAPUR", "7034", "3.15 to 5 (OLD)", 5.0),
        ("CHHINDWARA", "SAUSAR", "33kV Lodhikheda Feeder", "33F-5226", "LODHIKHEDA", "5226", "3.15 to 5 (OLD)", 5.0),
        ("CHHINDWARA", "SAUSAR", "33kV Borgaon Feeder", "33F-5222", "BORGAON", "5222", "3.15 to 5 (OLD)", 5.0),
        ("CHHINDWARA", "JUNNARDEO", "33kV Jamai Feeder", "33F-7358", "JAMAI", "7358", "3.15 MVA (OLD)", 3.15),
        ("CHHINDWARA", "JUNNARDEO", "33kV Barelipar Feeder", "33F-5770", "BARELIPAR", "5770", "3.15 MVA (OLD)", 3.15),
        ("CHHINDWARA", "JUNNARDEO", "33kV Chhindi Feeder", "33F-7356", "CHHINDI", "7356", "3.15 to 5 (OLD)", 5.0),
        ("CHHINDWARA", "JUNNARDEO", "33kV Tamiya Feeder", "33F-5780", "TAMIYA", "5780", "3.15 to 5 (OLD)", 5.0),
        ("CHHINDWARA", "AMARWARA", "33kV Harrai Feeder", "33F-5138", "HARRAI", "5138", "3.15 MVA (OLD)", 3.15),
        ("CHHINDWARA", "AMARWARA", "33kV Chhui Feeder", "33F-5132", "CHHUI", "5132", "5 to 10 (New)", 10.0),
        ("CHHINDWARA", "AMARWARA", "33kV Khiriti Feeder", "33F-5141", "KHIRITI", "5141", "3.15 to 5 (OLD)", 5.0),
        ("CHHINDWARA", "AMARWARA", "33kV SimariyaBhutai Feeder", "33F-5147", "SIMARIYABHUTAI", "5147", "3.15 to 5 (OLD)", 5.0),
        ("CHHINDWARA", "AMARWARA", "33kV Amarwada Feeder", "33F-5125", "AMARWADA", "5125", "3.15 MVA (OLD)", 3.15),
        ("CHHINDWARA", "AMARWARA", "33kV Surlakhapa Feeder", "33F-5149", "SURLAKHAPA", "5149", "5 to 10 (New)", 10.0),
        ("CHHINDWARA", "AMARWARA", "33kV Dhanora Feeder", "33F-5037", "DHANORA", "5037", "5 to 10 (New)", 10.0),
        ("CHHINDWARA", "AMARWARA", "33kV Singodi Feeder", "33F-5012", "SINGODI", "5012", "3.15 to 5 (OLD)", 5.0),
        ("JABALPUR CITY", "EAST, JABALPUR", "33kV Bhantaliya Feeder", "33F-1339", "BHANTALIYA", "1339", "8 to 10 (New)", 10.0),
        ("JABALPUR CITY", "EAST, JABALPUR", "33kV Gohalpur Feeder", "33F-1341", "GOHALPUR", "1341", "8 to 10 (New)", 10.0),
        ("JABALPUR CITY", "EAST, JABALPUR", "33kV Vinoba Feeder", "33F-1347", "VINOBA", "1347", "5 to 8 (old)", 8.0),
        ("JABALPUR CITY", "WEST, JABALPUR", "33kV Katanga Feeder", "33F-4257", "KATANGA", "4257", "8 to 10 (New)", 10.0),
        ("JABALPUR CITY", "NORTH, JABALPUR", "33kV Adhartal Feeder", "33F-5605", "ADHARTAL", "5605", "5 to 10 (New)", 10.0),
        ("JABALPUR CITY", "NORTH, JABALPUR", "33kV Richaai Feeder", "33F-5607", "RICHAAI", "5607", "5 MVA (OLD)", 5.0),
        ("JABALPUR CITY", "SOUTH, JABALPUR", "33kV Medical S/S Feeder", "33F-5529", "MEDICAL S/S", "5529", "5 to 8 (old)", 8.0),
        ("JABALPUR (O&M)", "SIHORA", "33kV Khirehni Feeder", "33F-6604", "KHIREHNI", "6604", "3.15 MVA (OLD)", 3.15),
        ("JABALPUR (O&M)", "PATAN", "33kV Patan Feeder", "33F-1653", "PATAN", "1653", "8 to 10 (New)", 10.0),
        ("KATNI", "KATNI CITY", "33kV Shanti Nagar Feeder", "33F-5011", "SHANTI NAGAR", "5011", "5 to 10 (New)", 10.0),
        ("KATNI", "SOUTH KATNI", "33kV Sleemnabad Feeder", "33F-1448", "SLEEMNABAD", "1448", "5 to 10 (New)", 10.0),
        ("NARSINGHPUR", "NARSINGHPUR", "33kV Barman Feeder", "33F-5075", "BARMAN", "5075", "5 to 10 (New)", 10.0),
        ("NARSINGHPUR", "GADARWARA", "33kV Bhama Feeder", "33F-5049", "BHAMA", "5049", "5 to 10 (New)", 10.0),
        ("SEONI", "SEONI", "33kV Gwari Feeder", "33F-4040", "GWARI", "4040", "5 to 10 (New)", 10.0),
        ("SEONI", "LAKHNADON", "33kV Lakhnadon Feeder", "33F-5859", "LAKHNADON", "5859", "5 to 10 (New)", 10.0),
        ("BALAGHAT", "BALAGHAT", "33kV Karanja Feeder", "33F-4171", "KARANJA", "4171", "5 to 10 (New)", 10.0),
        ("BALAGHAT", "WARASEONI", "33kV Dongarmali Feeder", "33F-5541", "DONGARMALI", "5541", "5 to 10 (New)", 10.0),
        ("REWA", "REWA CITY", "33kV Engg College Feeder", "33F-1708", "ENGINEERING COLLEGE", "1708", "5 to 10 (New)", 10.0),
        ("SATNA", "SATNA CITY", "33kV Kolgawan Feeder", "33F-5717", "KOLGAWAN", "5717", "5 to 10 (New)", 10.0),
        ("SATNA", "AMARPATAN", "33kV Ramnagar Feeder", "33F-5835", "RAMNAGAR", "5835", "5 to 10 (New)", 10.0),
        ("SAGAR", "REHLI", "33kV Chandpur Feeder", "33F-5496", "CHANDPUR", "5496", "5 to 10 (New)", 10.0),
        ("DAMOH", "NORTH DAMOH", "33kV Rajpura Feeder", "33F-5444", "RAJPURA", "5444", "3.15 MVA (OLD)", 3.15),
        ("SINGROLI", "WAIDHAN", "33kV Deosar Feeder", "33F-5924", "DEOSAR", "5924", "5 to 10 (New)", 10.0),
        ("SHAHDOL", "SHAHDOL", "33kV Beohari Feeder", "33F-5945", "BEOHARI", "5945", "5 to 10 (New)", 10.0),
        ("SIDHI", "SIDHI", "33kV Camping House Feeder", "33F-5802", "CAMPING HOUSE", "5802", "5 to 10 (New)", 10.0),
    ]
    fallback_records = []
    for cir, div, f33, c33, ss, ssc, ptr_r, mva in embedded_rows:
        fallback_records.append({
            "circle": cir, "division": div, "feeder_33kv": f33, "feeder_33kv_code": c33,
            "substation": ss, "ss_code": ssc, "ptr_mva_raw": ptr_r, "ptr_mva": mva,
            "feeder_11kv": "", "feeder_11kv_code": "", "peak_load_amps": round(mva * 28.0, 1)
        })
    return pd.DataFrame(fallback_records), ["wp 2025-26 (Embedded Sync)"], "EMBEDDED_FALLBACK"


def build_11kv_feeders_for_substation(df_ss_rows, ss_name, ss_code, ptr_mva, max_bus_sc_amps):
    """
    Builds the table of associated 11kV outgoing feeders and feeder codes for the selected 33/11kV Substation.
    Uses actual 11kV feeder rows from the Circle sheet if present; otherwise generates standardized
    substation feeders with SS-Code-linked feeder codes.
    """
    valid_11kv = df_ss_rows[df_ss_rows["feeder_11kv"].astype(str).str.strip() != ""]
    rows = []

    if not valid_11kv.empty:
        for idx, r in valid_11kv.iterrows():
            f_name = str(r["feeder_11kv"]).strip()
            f_code = str(r["feeder_11kv_code"]).strip() or f"{ss_code}-11F{len(rows)+1:02d}"
            f_load = float(r["peak_load_amps"]) if r["peak_load_amps"] > 0 else 95.0
            f_ct = recommend_standard_ct(f_load)
            f_51_p = round(f_load * 1.25, 0)
            f_sc = round(max_bus_sc_amps * 0.65, -1)
            rows.append({
                "11kV Feeder Code": f_code,
                "11kV Feeder Name": f_name,
                "Category": "Sheet Data",
                "CT Primary (A)": int(f_ct),
                "Peak Load (A)": round(f_load, 1),
                "Prospective 3Ph Fault (A)": f_sc,
                "51 Pickup (A)": f_51_p,
                "51 TMS": 0.08,
                "50 Enabled": True,
                "50 Pickup (A)": round(f_sc * 0.80, -1),
                "50 Delay (s)": 0.05,
                "51N Pickup (A)": round(f_ct * 0.15, 0),
                "51N TMS": 0.05
            })
    else:
        # Generate standard 11kV feeders scaled to the PTR MVA capacity and SS Code
        i_fl_total = (ptr_mva * 1e6) / (math.sqrt(3) * 11000.0)
        templates = [
            ("Town / Domestic", "Urban", 0.32, 0.70, 0.08),
            ("Industrial / Essential", "Industrial", 0.28, 0.75, 0.09),
            ("Abadi / Rural", "Rural", 0.22, 0.55, 0.07),
            ("Agriculture (DLF)", "Agriculture", 0.18, 0.50, 0.06),
        ]
        for i, (suffix, cat, load_share, sc_factor, tms_def) in enumerate(templates, start=1):
            f_load = max(35.0, round(i_fl_total * load_share, 0))
            f_ct = recommend_standard_ct(f_load)
            f_51_p = round(f_load * 1.25, 0)
            f_sc = round(max_bus_sc_amps * sc_factor, -1)
            rows.append({
                "11kV Feeder Code": f"{ss_code}-F0{i}",
                "11kV Feeder Name": f"11kV {ss_name.title()} {suffix}",
                "Category": cat,
                "CT Primary (A)": int(f_ct),
                "Peak Load (A)": f_load,
                "Prospective 3Ph Fault (A)": f_sc,
                "51 Pickup (A)": f_51_p,
                "51 TMS": tms_def,
                "50 Enabled": True,
                "50 Pickup (A)": round(f_sc * 0.80, -1),
                "50 Delay (s)": 0.05,
                "51N Pickup (A)": round(f_ct * 0.15, 0),
                "51N TMS": 0.05
            })

    return pd.DataFrame(rows)


# ---------------------------------------------------------
# 2. PROTECTION MATH & COORDINATION FUNCTIONS
# ---------------------------------------------------------
def get_default_ptr_z_pct(ptr_mva):
    """Returns standard IS 2026 percentage impedance (%Z) for 33/11kV PTRs."""
    if ptr_mva <= 3.15:
        return 6.25
    elif ptr_mva <= 5.0:
        return 7.15
    elif ptr_mva <= 8.0:
        return 8.35
    return 9.00


def calc_idmt_time(fault_current, pickup_current, tms, curve_constant):
    if not pickup_current or pickup_current <= 0 or not tms or tms <= 0:
        return None
    psm = fault_current / pickup_current
    if psm <= 1.0:
        return None
    return (curve_constant / ((psm ** 0.02) - 1.0)) * tms


def calc_required_tms(fault_current, pickup_current, target_time_sec, curve_constant, min_tms=0.025, max_tms=1.2):
    if not pickup_current or pickup_current <= 0 or fault_current <= pickup_current or target_time_sec <= 0:
        return None
    psm = fault_current / pickup_current
    raw_tms = (target_time_sec * ((psm ** 0.02) - 1.0)) / curve_constant
    clamped_tms = max(min_tms, min(max_tms, raw_tms))
    return {"psm": round(psm, 2), "raw_tms": round(raw_tms, 3), "recommended_tms": round(clamped_tms, 3)}


def calc_composite_time(fault_current, idmt_pickup, tms, curve_constant, hs_enabled=False, hs_pickup=None, hs_delay_sec=0.05):
    t_idmt = calc_idmt_time(fault_current, idmt_pickup, tms, curve_constant)
    if hs_enabled and hs_pickup and hs_pickup > 0 and fault_current >= hs_pickup:
        return min(t_idmt, hs_delay_sec) if t_idmt is not None else hs_delay_sec
    return t_idmt


def calculate_ptr_parameters(v_hv_kv, v_lv_kv, ptr_mva, ptr_z_pct, ngr_ohms, ct_lv_pri, ct_hv_pri):
    turn_ratio = v_hv_kv / v_lv_kv
    v_lv_ln_volts = (v_lv_kv * 1000.0) / math.sqrt(3)
    i_fl_lv = (ptr_mva * 1e6) / (math.sqrt(3) * v_lv_kv * 1000.0)
    i_fl_hv = (ptr_mva * 1e6) / (math.sqrt(3) * v_hv_kv * 1000.0)

    z_pu = ptr_z_pct / 100.0
    i_sc_3ph_lv = i_fl_lv / z_pu
    i_sc_3ph_hv = i_fl_hv / z_pu

    z_ptr_ohms = ((v_lv_kv ** 2) / ptr_mva) * z_pu
    r_ptr_ohms = z_ptr_ohms / math.sqrt(1 + 10**2)
    x_ptr_ohms = r_ptr_ohms * 10.0
    dyn11_lg_hv_factor = 1.0 / (math.sqrt(3) * turn_ratio)

    if ngr_ohms and ngr_ohms > 0:
        max_ef_lv = v_lv_ln_volts / ngr_ohms
        rec_51g_pickup = max_ef_lv * 0.20
    else:
        max_ef_lv = i_sc_3ph_lv
        rec_51g_pickup = ct_lv_pri * 0.20

    max_ef_reflected_hv_line = max_ef_lv * dyn11_lg_hv_factor

    return {
        "turn_ratio": round(turn_ratio, 3),
        "v_lv_ln_volts": round(v_lv_ln_volts, 1),
        "r_ptr_ohms": r_ptr_ohms,
        "x_ptr_ohms": x_ptr_ohms,
        "z_ptr_ohms": round(z_ptr_ohms, 4),
        "i_fl_lv_A": round(i_fl_lv, 2),
        "i_fl_hv_A": round(i_fl_hv, 2),
        "max_3ph_sc_lv_A": round(i_sc_3ph_lv, 1),
        "max_3ph_sc_hv_A": round(i_sc_3ph_hv, 1),
        "max_ef_lv_A": round(max_ef_lv, 2),
        "max_ef_reflected_hv_A": round(max_ef_reflected_hv_line, 2),
        "rec_51g_pickup_A": round(rec_51g_pickup, 2),
        "rec_51g_plug_pct": round((rec_51g_pickup / ct_lv_pri) * 100.0, 2),
        "dyn11_lg_hv_factor": dyn11_lg_hv_factor
    }


def calc_fault_at_distance_km(dist_km, v_ln_volts, r_ptr, x_ptr, r1_km, x1_km):
    r_total = r_ptr + (r1_km * dist_km)
    x_total = x_ptr + (x1_km * dist_km)
    z_total = math.hypot(r_total, x_total)
    return v_ln_volts / z_total if z_total > 0 else 0.0


def calc_ansi_50_reach_km(hs_pickup_amps, v_ln_volts, r_ptr, x_ptr, r1_km, x1_km, total_length_km):
    if not hs_pickup_amps or hs_pickup_amps <= 0:
        return 0.0, 0.0
    z_target = v_ln_volts / hs_pickup_amps
    z_ptr = math.hypot(r_ptr, x_ptr)
    if z_target <= z_ptr:
        return 0.0, 0.0
    a = (r1_km ** 2) + (x1_km ** 2)
    b = 2.0 * ((r_ptr * r1_km) + (x_ptr * x1_km))
    c = (r_ptr ** 2) + (x_ptr ** 2) - (z_target ** 2)
    if a <= 0:
        return 0.0, 0.0
    discriminant = (b ** 2) - (4.0 * a * c)
    if discriminant < 0:
        return 0.0, 0.0
    reach_km = (-b + math.sqrt(discriminant)) / (2.0 * a)
    reach_pct = (reach_km / total_length_km) * 100.0 if total_length_km > 0 else 0.0
    return round(max(0.0, reach_km), 2), round(max(0.0, reach_pct), 1)


def evaluate_ct_saturation(
    zone_name, ct_primary, ct_secondary, rated_va, ct_class_str,
    r_ct_ohms, lead_length_m, lead_sqmm, relay_va, max_fault_primary_amps
):
    alf_rated = CT_CLASS_PRESETS.get(ct_class_str, 10.0)
    i_sec_sq = ct_secondary ** 2
    r_b_rated = rated_va / i_sec_sq
    r_lead_loop = (2.0 * lead_length_m * 0.0178) / lead_sqmm if lead_sqmm > 0 else 0.0
    r_relay = relay_va / i_sec_sq
    r_b_actual = r_lead_loop + r_relay
    z_total_rated = r_ct_ohms + r_b_rated
    z_total_actual = r_ct_ohms + r_b_actual
    v_knee = alf_rated * ct_secondary * z_total_rated
    alf_effective = alf_rated * (z_total_rated / z_total_actual) if z_total_actual > 0 else alf_rated
    max_unsat_primary_amps = alf_effective * ct_primary
    actual_fault_multiple = max_fault_primary_amps / ct_primary if ct_primary > 0 else 0.0
    i_fault_sec = max_fault_primary_amps * (ct_secondary / ct_primary) if ct_primary > 0 else 0.0
    v_fault_req = i_fault_sec * z_total_actual
    safety_ratio = alf_effective / actual_fault_multiple if actual_fault_multiple > 0 else 99.0
    if safety_ratio >= 1.2:
        verdict = "✅ PASS (Linear / Unsaturated)"
    elif safety_ratio >= 1.0:
        verdict = "⚠️ MARGINAL (<20% Headroom)"
    else:
        verdict = "❌ SATURATES (Upgrade VA / Class)"

    return {
        "Zone": zone_name,
        "CT Ratio & Class": f"{int(ct_primary)}/{int(ct_secondary)}A ({rated_va:.0f}VA {ct_class_str})",
        "Rated Burden Z (Ω)": round(r_b_rated, 3),
        "Actual Loop Z (Rct+Lead+Relay Ω)": round(z_total_actual, 3),
        "Rated ALF": alf_rated,
        "Effective ALF (ALF_eff)": round(alf_effective, 1),
        "Max Unsat. Current (A)": round(max_unsat_primary_amps, 0),
        "Max Zone Fault (A)": round(max_fault_primary_amps, 0),
        "Actual Fault Multiple (x In)": round(actual_fault_multiple, 1),
        "Knee EMF Vk / Req Vf (V)": f"{v_knee:.1f} V / {v_fault_req:.1f} V",
        "CT Saturation Verdict": verdict,
    }


def build_curve_trace(fc_list, idmt_pickup, tms, c_const, hs_en, hs_pickup, hs_delay):
    pts = list(fc_list)
    if hs_en and hs_pickup and hs_pickup > pts[0] and hs_pickup <= pts[-1]:
        pts.extend([hs_pickup - 0.1, hs_pickup])
        pts = sorted(list(set(pts)))
    x_vals, y_vals = [], []
    for fc in pts:
        t = calc_composite_time(fc, idmt_pickup, tms, c_const, hs_en, hs_pickup, hs_delay)
        if t is not None:
            x_vals.append(fc)
            y_vals.append(t)
    return x_vals, y_vals


def generate_html_commissioning_report(
    circle_name, division_name, feeder_33kv, feeder_33kv_code, substation_name, ss_code,
    engineer_name, ptr_mva, ptr_z_pct, ngr_ohms, ptr_data, df_schedule, df_audit, cti_ms
):
    now_str = datetime.now().strftime("%d-%b-%Y %H:%M")
    schedule_html = df_schedule.to_html(index=False, border=1, classes="report-table")
    audit_html = df_audit.to_html(index=False, border=1, classes="report-table")

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <title>Relay Setting Report - {substation_name} ({ss_code})</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 24px; color: #1a1a1a; }}
        h1 {{ color: #0d47a1; margin-bottom: 4px; font-size: 20px; text-transform: uppercase; }}
        h2 {{ color: #1565c0; margin-top: 20px; margin-bottom: 8px; font-size: 15px; border-bottom: 2px solid #0d47a1; padding-bottom: 4px; }}
        .meta-box {{ background: #f5f7fa; border: 1px solid #cbd5e1; padding: 12px; border-radius: 6px; margin-bottom: 16px; font-size: 12.5px; }}
        .meta-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }}
        .report-table {{ width: 100%; border-collapse: collapse; font-size: 11.5px; margin-top: 8px; }}
        .report-table th {{ background-color: #0d47a1; color: white; padding: 6px; text-align: center; }}
        .report-table td {{ padding: 5px; text-align: center; border: 1px solid #cbd5e1; }}
        .sig-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px; margin-top: 48px; text-align: center; font-size: 13px; font-weight: bold; }}
        .sig-line {{ border-top: 1px solid #333; padding-top: 6px; margin-top: 40px; }}
    </style>
    </head>
    <body>
        <h1>⚡ MPPKVVCL Protective Relay Setting & Substation Coordination Report</h1>
        <p style="margin-top:0; font-size:12.5px; color:#555;">Generated on: {now_str} | Standard: IEC 60255 / ANSI C37.2 (50/51/50N/51N/51G)</p>

        <div class="meta-box">
            <div class="meta-grid">
                <div><strong>Circle:</strong> {circle_name}</div>
                <div><strong>Division:</strong> {division_name}</div>
                <div><strong>33/11kV Substation (Code):</strong> {substation_name} ({ss_code})</div>
                <div><strong>Associated 33kV Feeder (Code):</strong> {feeder_33kv} ({feeder_33kv_code})</div>
                <div><strong>PTR Rating & Vector:</strong> {ptr_mva} MVA, 33/11kV, Dyn11</div>
                <div><strong>PTR Impedance (%Z):</strong> {ptr_z_pct}% (Z_LV = {ptr_data['z_ptr_ohms']} Ω)</div>
                <div><strong>PTR NGR Resistance:</strong> {ngr_ohms} Ω (Max EF = {ptr_data['max_ef_lv_A']} A)</div>
                <div><strong>Max 3Ph Bus Fault (11kV / 33kV):</strong> {ptr_data['max_3ph_sc_lv_A']} A / {ptr_data['max_3ph_sc_hv_A']} A</div>
                <div><strong>Prepared By (CTI = {cti_ms:.0f}ms):</strong> {engineer_name}</div>
            </div>
        </div>

        <h2>1. Master Substation Relay Setting Schedule (PTR HV, PTR LV & Associated 11kV Feeders)</h2>
        {schedule_html}

        <h2>2. Multi-Feeder Coordination & Grading Margin Audit</h2>
        {audit_html}

        <div class="sig-grid">
            <div><div class="sig-line">Assistant Engineer (STM)</div></div>
            <div><div class="sig-line">Executive Engineer (O&M / M&C)</div></div>
            <div><div class="sig-line">Executive Engineer (STM Division)</div></div>
        </div>
    </body>
    </html>
    """


# ---------------------------------------------------------
# 3. STREAMLIT APP CONFIG & GLOBAL SIDEBAR (CIRCLE -> S/S)
# ---------------------------------------------------------
st.set_page_config(page_title="MPPKVVCL Relay Coordination Suite", layout="wide")

with st.sidebar:
    st.title("⚡ MPPKVVCL Relay Suite")
    app_page = st.radio(
        "Select Module Page:",
        [
            "Page 1: Single-Feeder Deep Dive (Reach & CT Saturation)",
            "Page 2: Multi-Feeder PTR Coordination, Auto-TMS & Report"
        ],
        index=1
    )

    st.divider()
    st.header("1. Company Peak Load Database")
    sheet_url_input = st.text_input("Google Sheet Backend URL", value=DEFAULT_PEAK_LOAD_SHEET_URL)
    uploaded_xlsx = st.file_uploader("Or Upload Local Peak Load Sheet (.xlsx/.csv)", type=["xlsx", "csv"])

    if uploaded_xlsx is not None:
        if uploaded_xlsx.name.endswith(".csv"):
            df_raw_up = pd.read_csv(uploaded_xlsx, header=None)
            db_df = normalize_sheet_dataframe(df_raw_up, fallback_circle="UPLOADED")
            db_status = "LOCAL_CSV"
        else:
            xls_up = pd.ExcelFile(uploaded_xlsx)
            up_dfs = []
            for t_name in xls_up.sheet_names:
                df_t = pd.read_excel(xls_up, sheet_name=t_name, header=None)
                df_n = normalize_sheet_dataframe(df_t, fallback_circle=t_name)
                if not df_n.empty:
                    if "wp" not in t_name.lower() and "sheet" not in t_name.lower():
                        df_n["circle"] = t_name.strip().upper()
                    up_dfs.append(df_n)
            db_df = pd.concat(up_dfs, ignore_index=True) if up_dfs else pd.DataFrame()
            db_status = "LOCAL_XLSX"
    else:
        db_df, loaded_tabs, db_status = load_company_peak_load_database(sheet_url_input)

    if db_status == "LIVE_GSHEET":
        st.success(f"✅ Live Google Sheet Connected ({len(db_df)} substations/records)")
    elif db_status.startswith("LOCAL"):
        st.success(f"✅ Local Sheet Loaded ({len(db_df)} records)")
    else:
        st.info("ℹ️ Using synced Peak Loading FY 2026-27 database snapshot (Share sheet as 'Anyone with link can view' for live multi-tab sync).")

    # ---------------------------------------------------------
    # GLOBAL HIERARCHICAL SELECTOR: CIRCLE -> DIVISION -> SUBSTATION
    # (Appears on EVERY page of the Web App)
    # ---------------------------------------------------------
    st.header("2. Select Circle & 33/11kV S/S")
    circles_list = sorted(db_df["circle"].dropna().unique().tolist())
    default_circle_idx = circles_list.index("CHHINDWARA") if "CHHINDWARA" in circles_list else 0
    selected_circle = st.selectbox("Select Circle Name (Sheet Tab)", circles_list, index=default_circle_idx)

    df_circle = db_df[db_df["circle"] == selected_circle]
    divisions_list = ["ALL DIVISIONS"] + sorted(df_circle["division"].dropna().unique().tolist())
    selected_division_filter = st.selectbox("Filter by Division", divisions_list, index=0)

    if selected_division_filter != "ALL DIVISIONS":
        df_div = df_circle[df_circle["division"] == selected_division_filter]
    else:
        df_div = df_circle

    # Build formatted Substation selector options: "SUBSTATION NAME (Code: 5155 | 3.15 MVA)"
    ss_unique_df = df_div.drop_duplicates(subset=["substation", "ss_code"]).sort_values("substation")
    ss_options = [
        f"{row['substation']} (SS Code: {row['ss_code']} | {row['ptr_mva']} MVA)"
        for _, row in ss_unique_df.iterrows()
    ]
    selected_ss_label = st.selectbox("Select 33/11kV Substation", ss_options, index=0)
    selected_ss_row = ss_unique_df.iloc[ss_options.index(selected_ss_label)]

    active_ss_name = str(selected_ss_row["substation"])
    active_ss_code = str(selected_ss_row["ss_code"])
    active_division = str(selected_ss_row["division"])
    active_33kv_fdr = str(selected_ss_row["feeder_33kv"])
    active_33kv_code = str(selected_ss_row["feeder_33kv_code"])
    sheet_ptr_mva = float(selected_ss_row["ptr_mva"])
    sheet_ptr_raw = str(selected_ss_row["ptr_mva_raw"])

    df_selected_ss_rows = df_div[
        (df_div["substation"] == active_ss_name) & (df_div["ss_code"] == active_ss_code)
    ]

    st.caption(
        f"📍 **Division:** `{active_division}`  \n"
        f"⚡ **33kV Feeder:** `{active_33kv_fdr}` (`{active_33kv_code}`)  \n"
        f"🏭 **Sheet PTR Spec:** `{sheet_ptr_raw}` (`{sheet_ptr_mva} MVA`)"
    )

    st.header("3. PTR & Breaker (52) Specs")
    engineer_name = st.text_input("Engineer Name", value="Pallav Swarnkar")
    ptr_mva = st.number_input("Active PTR Rating (MVA)", min_value=1.0, max_value=100.0, value=sheet_ptr_mva, step=0.05)
    ptr_z_pct = st.number_input("PTR Impedance (%Z)", min_value=1.0, max_value=25.0, value=get_default_ptr_z_pct(ptr_mva), step=0.05)
    ngr_ohms = st.number_input("PTR Neutral Earth Resistance (NGR Ω) [0 = Solid]", min_value=0.0, max_value=500.0, value=40.0, step=1.0)
    cb_open_ms = st.number_input("VCB (52) Interrupting Time (ms)", min_value=20.0, max_value=200.0, value=60.0, step=5.0)
    overshoot_ms = st.number_input("Relay Overshoot & Safety Margin (ms)", min_value=10.0, max_value=200.0, value=40.0, step=5.0)
    total_cti_sec = (cb_open_ms + overshoot_ms) / 1000.0

# Pre-calculate PTR Parameters & Associated 11kV Feeders for the selected Substation
default_lv_ct = recommend_standard_ct((ptr_mva * 1e6) / (math.sqrt(3) * 11000.0))
default_hv_ct = recommend_standard_ct((ptr_mva * 1e6) / (math.sqrt(3) * 33000.0))
ptr_data = calculate_ptr_parameters(33.0, 11.0, ptr_mva, ptr_z_pct, ngr_ohms, default_lv_ct, default_hv_ct)

associated_11kv_df = build_11kv_feeders_for_substation(
    df_selected_ss_rows, active_ss_name, active_ss_code, ptr_mva, ptr_data["max_3ph_sc_lv_A"]
)

# Top Context Banner (Displayed on BOTH Pages)
st.markdown(
    f"### 🏛️ Circle: `{selected_circle}` ➔ Division: `{active_division}` ➔ "
    f"33kV Feeder: `{active_33kv_fdr} ({active_33kv_code})` ➔ "
    f"33/11kV S/S: `{active_ss_name} (Code: {active_ss_code} | {ptr_mva} MVA)`"
)


# =========================================================
# PAGE 1: SINGLE-FEEDER DEEP DIVE (REACH & CT SATURATION)
# =========================================================
if app_page.startswith("Page 1"):
    st.subheader("⚡ Page 1: Single-Feeder 3-Zone Coordination, 11kV Line Reach & CT Saturation")

    # Select which associated 11kV feeder from this Substation to inspect in detail
    fdr_11_labels = [
        f"{r['11kV Feeder Name']} (Code: {r['11kV Feeder Code']} | Peak: {r['Peak Load (A)']} A)"
        for _, r in associated_11kv_df.iterrows()
    ]
    sel_11_label = st.selectbox("Select Associated 11kV Outgoing Feeder for Deep-Dive Analysis:", fdr_11_labels, index=0)
    sel_11_row = associated_11kv_df.iloc[fdr_11_labels.index(sel_11_label)]

    with st.sidebar:
        st.header("Page 1: Line & CT Loop Specs")
        conductor_choice = st.selectbox("Conductor / Cable Type", list(CONDUCTOR_PRESETS.keys()), index=0)
        default_r1 = CONDUCTOR_PRESETS[conductor_choice]["R1"]
        default_x1 = CONDUCTOR_PRESETS[conductor_choice]["X1"]
        is_custom = conductor_choice == "Custom R + jX"
        r1_per_km = st.number_input("Positive Seq R1 (Ω/km)", min_value=0.01, max_value=5.0, value=default_r1, step=0.01, format="%.3f", disabled=not is_custom)
        x1_per_km = st.number_input("Positive Seq X1 (Ω/km)", min_value=0.01, max_value=5.0, value=default_x1, step=0.01, format="%.3f", disabled=not is_custom)
        feeder_length_km = st.number_input("Total 11kV Feeder Length (km)", min_value=0.5, max_value=100.0, value=12.0, step=0.5)
        target_reach_pct = st.slider("Target ANSI 50 Protected Reach (%)", min_value=50, max_value=90, value=80, step=5)

        ct_sec_rating = st.selectbox("CT Secondary Rating (A)", [5.0, 1.0], index=0)
        lead_sqmm = st.selectbox("Control Cable Size (sq.mm Cu)", [2.5, 4.0, 6.0], index=0)
        lead_length_m = st.number_input("One-Way CT-to-Relay Cable Length (m)", min_value=1.0, max_value=200.0, value=25.0, step=5.0)
        r_ct_winding = st.number_input("CT Secondary Winding Rct (Ω)", min_value=0.05, max_value=5.0, value=0.25, step=0.05)
        relay_burden_va = st.number_input("Numerical Relay Burden (VA)", min_value=0.05, max_value=10.0, value=0.50, step=0.10)

    target_reach_km = feeder_length_km * (target_reach_pct / 100.0)
    fault_at_target_reach = calc_fault_at_distance_km(
        target_reach_km, ptr_data["v_lv_ln_volts"], ptr_data["r_ptr_ohms"], ptr_data["x_ptr_ohms"], r1_per_km, x1_per_km
    )
    fault_at_feeder_end = calc_fault_at_distance_km(
        feeder_length_km, ptr_data["v_lv_ln_volts"], ptr_data["r_ptr_ohms"], ptr_data["x_ptr_ohms"], r1_per_km, x1_per_km
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(f"#### Zone 1: `{sel_11_row['11kV Feeder Name']}` (`{sel_11_row['11kV Feeder Code']}`)")
        st.caption(f"Rec. {target_reach_pct}% Zone-1 50 Pickup: **{fault_at_target_reach:.0f} A** ({target_reach_km:.1f} km)")
        fdr_ct = st.number_input("Feeder CT Primary (A)", value=float(sel_11_row["CT Primary (A)"]), step=50.0)
        c1_a, c1_b = st.columns(2)
        fdr_ct_va = c1_a.selectbox("Feeder CT VA", [10.0, 15.0, 30.0], index=1)
        fdr_ct_class = c1_b.selectbox("Feeder CT Class", list(CT_CLASS_PRESETS.keys()), index=0)
        fdr_curve_name = st.selectbox("Feeder IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0, key="fdr_c")
        fdr_oc_pickup = st.number_input("Feeder 51 OC Pickup (A)", value=float(sel_11_row["51 Pickup (A)"]), step=10.0)
        fdr_oc_tms = st.number_input("Feeder 51 OC TMS", value=float(sel_11_row["51 TMS"]), step=0.01, format="%.2f")
        fdr_ef_pickup = st.number_input("Feeder 51N EF Pickup (A)", value=float(sel_11_row["51N Pickup (A)"]), step=5.0)
        fdr_ef_tms = st.number_input("Feeder 51N EF TMS", value=float(sel_11_row["51N TMS"]), step=0.01, format="%.2f")
        fdr_50_en = st.checkbox("Enable Feeder ANSI 50", value=True)
        fdr_50_pickup = st.number_input("Feeder 50 High-Set Pickup (A)", value=float(round(fault_at_target_reach, -1)), step=50.0, disabled=not fdr_50_en)
        fdr_50_delay = st.number_input("Feeder 50 Operating Time (s)", min_value=0.02, max_value=2.0, value=0.05, step=0.01, format="%.2f", disabled=not fdr_50_en)
        fdr_50n_en = st.checkbox("Enable Feeder ANSI 50N", value=True)
        fdr_50n_pickup = st.number_input("Feeder 50N High-Set Pickup (A)", value=80.0, step=5.0, disabled=not fdr_50n_en)
        fdr_50n_delay = st.number_input("Feeder 50N Operating Time (s)", min_value=0.02, max_value=2.0, value=0.05, step=0.01, format="%.2f", disabled=not fdr_50n_en)

    with col2:
        st.markdown(f"#### Zone 2: 11kV PTR LV (`{active_ss_name}`)")
        st.caption(f"PTR Full Load: **{ptr_data['i_fl_lv_A']} A** at 11kV")
        lv_ct = st.number_input("PTR LV CT Primary (A)", value=float(default_lv_ct), step=50.0)
        c2_a, c2_b = st.columns(2)
        lv_ct_va = c2_a.selectbox("PTR LV CT VA", [10.0, 15.0, 30.0], index=1)
        lv_ct_class = c2_b.selectbox("PTR LV CT Class", list(CT_CLASS_PRESETS.keys()), index=2)
        lv_curve_name = st.selectbox("PTR LV IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0, key="lv_c")
        lv_oc_pickup = st.number_input("PTR LV 51 OC Pickup (A)", value=float(round(ptr_data["i_fl_lv_A"] * 1.15, -1)), step=10.0)
        lv_oc_tms = st.number_input("PTR LV 51 OC TMS", value=0.15, step=0.01, format="%.2f")
        lv_ef_pickup = st.number_input("PTR Neutral 51G Pickup (A)", value=float(round(ptr_data["rec_51g_pickup_A"], 0)), step=5.0)
        lv_ef_tms = st.number_input("PTR Neutral 51G TMS", value=0.12, step=0.01, format="%.2f")
        lv_50_en = st.checkbox("Enable PTR LV ANSI 50", value=False)
        lv_50_pickup = st.number_input("PTR LV 50 Pickup (A)", value=float(round(ptr_data["max_3ph_sc_lv_A"] * 0.85, -1)), step=100.0, disabled=not lv_50_en)
        lv_50_delay = st.number_input("PTR LV 50 Delay (s)", min_value=0.02, max_value=2.0, value=0.20, step=0.01, format="%.2f", disabled=not lv_50_en)
        lv_50g_en = st.checkbox("Enable PTR Neutral ANSI 50G", value=False)
        lv_50g_pickup = st.number_input("PTR Neutral 50G Pickup (A)", value=130.0, step=5.0, disabled=not lv_50g_en)
        lv_50g_delay = st.number_input("PTR Neutral 50G Delay (s)", min_value=0.02, max_value=2.0, value=0.20, step=0.01, format="%.2f", disabled=not lv_50g_en)

    with col3:
        st.markdown(f"#### Zone 3: 33kV PTR HV (`{active_33kv_fdr}`)")
        st.caption(f"PTR Full Load: **{ptr_data['i_fl_hv_A']} A** at 33kV")
        hv_ct = st.number_input("PTR HV CT Primary (A at 33kV)", value=float(default_hv_ct), step=50.0)
        c3_a, c3_b = st.columns(2)
        hv_ct_va = c3_a.selectbox("PTR HV CT VA", [10.0, 15.0, 30.0], index=1)
        hv_ct_class = c3_b.selectbox("PTR HV CT Class", list(CT_CLASS_PRESETS.keys()), index=2)
        hv_curve_name = st.selectbox("PTR HV IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0, key="hv_c")
        hv_oc_pickup_33kv = st.number_input("PTR HV 51 OC Pickup (A at 33kV)", value=float(round(ptr_data["i_fl_hv_A"] * 1.25, 0)), step=5.0)
        hv_oc_tms = st.number_input("PTR HV 51 OC TMS", value=0.22, step=0.01, format="%.2f")
        hv_50_en = st.checkbox("Enable PTR HV ANSI 50 (33kV)", value=True)
        hv_50_pickup_33kv = st.number_input("PTR HV 50 Pickup (A at 33kV)", value=float(round(ptr_data["max_3ph_sc_hv_A"] * 1.25, -1)), step=50.0, disabled=not hv_50_en)
        hv_50_delay = st.number_input("PTR HV 50 Operating Time (s)", min_value=0.02, max_value=2.0, value=0.05, step=0.01, format="%.2f", disabled=not hv_50_en)

    actual_reach_km, actual_reach_pct = calc_ansi_50_reach_km(
        fdr_50_pickup if fdr_50_en else 0.0,
        ptr_data["v_lv_ln_volts"], ptr_data["r_ptr_ohms"], ptr_data["x_ptr_ohms"],
        r1_per_km, x1_per_km, feeder_length_km
    )
    ct_eval_z1 = evaluate_ct_saturation(f"Zone 1: {sel_11_row['11kV Feeder Code']} CT", fdr_ct, ct_sec_rating, fdr_ct_va, fdr_ct_class, r_ct_winding, lead_length_m, lead_sqmm, relay_burden_va, ptr_data["max_3ph_sc_lv_A"])
    ct_eval_z2 = evaluate_ct_saturation(f"Zone 2: {active_ss_name} PTR LV CT", lv_ct, ct_sec_rating, lv_ct_va, lv_ct_class, r_ct_winding, lead_length_m, lead_sqmm, relay_burden_va, ptr_data["max_3ph_sc_lv_A"])
    ct_eval_z3 = evaluate_ct_saturation(f"Zone 3: {active_ss_name} PTR HV CT", hv_ct, ct_sec_rating, hv_ct_va, hv_ct_class, r_ct_winding, lead_length_m, lead_sqmm, relay_burden_va, ptr_data["max_3ph_sc_hv_A"])

    c_fdr = CURVE_CONSTANTS[fdr_curve_name]
    c_lv = CURVE_CONSTANTS[lv_curve_name]
    c_hv = CURVE_CONSTANTS[hv_curve_name]
    hv_oc_pickup_referred_11kv_3ph = hv_oc_pickup_33kv * ptr_data["turn_ratio"]
    hv_50_pickup_referred_11kv_3ph = hv_50_pickup_33kv * ptr_data["turn_ratio"]
    hv_oc_pickup_referred_11kv_lg = hv_oc_pickup_33kv * ptr_data["turn_ratio"] * math.sqrt(3)
    hv_50_pickup_referred_11kv_lg = hv_50_pickup_33kv * ptr_data["turn_ratio"] * math.sqrt(3)

    fig = make_subplots(
        rows=1, cols=3,
        subplot_titles=("1. Phase OC (ANSI 50+51) TCC", "2. Earth Fault (50N/51N + 51G) TCC", "3. 11kV Fault vs. Distance (km)")
    )
    max_3ph = int(ptr_data["max_3ph_sc_lv_A"])
    max_ef = int(ptr_data["max_ef_lv_A"])

    fc_oc_range = list(range(20, int(max_3ph * 1.25) + 25, 25))
    fdr_oc_x, fdr_oc_y = build_curve_trace(fc_oc_range, fdr_oc_pickup, fdr_oc_tms, c_fdr, fdr_50_en, fdr_50_pickup, fdr_50_delay)
    lv_oc_x, lv_oc_y = build_curve_trace(fc_oc_range, lv_oc_pickup, lv_oc_tms, c_lv, lv_50_en, lv_50_pickup, lv_50_delay)
    hv_oc_x, hv_oc_y = build_curve_trace(fc_oc_range, hv_oc_pickup_referred_11kv_3ph, hv_oc_tms, c_hv, hv_50_en, hv_50_pickup_referred_11kv_3ph, hv_50_delay)

    fig.add_trace(go.Scatter(x=fdr_oc_x, y=fdr_oc_y, name=f"Z1: {sel_11_row['11kV Feeder Code']} (51+50)", line=dict(color="#d32f2f", width=2.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=lv_oc_x, y=lv_oc_y, name="Z2: PTR LV (51+50)", line=dict(color="#0288d1", width=2.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=hv_oc_x, y=hv_oc_y, name="Z3: PTR HV (Ref 11kV)", line=dict(color="#388e3c", width=2.5, dash="dash")), row=1, col=1)
    fig.add_vline(x=max_3ph, line_dash="solid", line_color="black", annotation_text=f"0km Bus: {max_3ph}A", row=1, col=1)

    step_ef = max(2, int(max_ef / 120))
    fc_ef_range = list(range(5, max_ef + step_ef, step_ef))
    fdr_ef_x, fdr_ef_y = build_curve_trace(fc_ef_range, fdr_ef_pickup, fdr_ef_tms, c_fdr, fdr_50n_en, fdr_50n_pickup, fdr_50n_delay)
    lv_ef_x, lv_ef_y = build_curve_trace(fc_ef_range, lv_ef_pickup, lv_ef_tms, c_lv, lv_50g_en, lv_50g_pickup, lv_50g_delay)
    hv_lg_x, hv_lg_y = build_curve_trace(fc_ef_range, hv_oc_pickup_referred_11kv_lg, hv_oc_tms, c_hv, hv_50_en, hv_50_pickup_referred_11kv_lg, hv_50_delay)

    fig.add_trace(go.Scatter(x=fdr_ef_x, y=fdr_ef_y, name=f"Z1: {sel_11_row['11kV Feeder Code']} (51N+50N)", line=dict(color="#d32f2f", width=2.5, dash="dot")), row=1, col=2)
    fig.add_trace(go.Scatter(x=lv_ef_x, y=lv_ef_y, name="Z2: PTR Neutral (51G)", line=dict(color="#0288d1", width=2.5, dash="dot")), row=1, col=2)
    fig.add_trace(go.Scatter(x=hv_lg_x, y=hv_lg_y, name="Z3: 33kV Dyn11 L-G", line=dict(color="#388e3c", width=2, dash="dashdot")), row=1, col=2)
    fig.add_vline(x=max_ef, line_dash="solid", line_color="purple", annotation_text=f"NGR Max: {max_ef}A", row=1, col=2)

    dist_points = [round(i * (feeder_length_km / 100.0), 2) for i in range(0, 101)]
    fault_profile_y = [calc_fault_at_distance_km(d, ptr_data["v_lv_ln_volts"], ptr_data["r_ptr_ohms"], ptr_data["x_ptr_ohms"], r1_per_km, x1_per_km) for d in dist_points]
    fig.add_trace(go.Scatter(x=dist_points, y=fault_profile_y, name="3Ph Fault vs km", line=dict(color="#6a1b9a", width=3)), row=1, col=3)
    if fdr_50_en:
        fig.add_hline(y=fdr_50_pickup, line_dash="dash", line_color="#d32f2f", annotation_text=f"50 Pickup: {fdr_50_pickup:.0f}A", row=1, col=3)
        fig.add_vline(x=actual_reach_km, line_dash="dot", line_color="green", annotation_text=f"Reach: {actual_reach_km}km ({actual_reach_pct}%)", row=1, col=3)

    fig.update_xaxes(type="log", title_text="Fault Current (A)", row=1, col=1)
    fig.update_yaxes(type="log", title_text="Operating Time (s)", range=[-1.6, 2.0], row=1, col=1)
    fig.update_xaxes(type="log", title_text="Fault Current (A)", row=1, col=2)
    fig.update_yaxes(type="log", title_text="Operating Time (s)", range=[-1.6, 2.0], row=1, col=2)
    fig.update_xaxes(title_text="Feeder Distance (km)", row=1, col=3)
    fig.update_yaxes(title_text="3-Phase Fault Current (A)", row=1, col=3)
    fig.update_layout(height=520, legend=dict(orientation="h", yanchor="bottom", y=-0.28, xanchor="center", x=0.5))
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("🧲 3-Zone CT Saturation & Effective Accuracy Limit Factor (ALF_eff) Verification")
    st.dataframe(pd.DataFrame([ct_eval_z1, ct_eval_z2, ct_eval_z3]), use_container_width=True)


# =========================================================
# PAGE 2: MULTI-FEEDER PTR COORDINATION, AUTO-TMS & REPORT
# =========================================================
else:
    st.subheader("🏢 Page 2: Multi-Feeder Substation Coordination, Prospective Fault Calculator & Report")

    # ---------------------------------------------------------
    # SECTION A: REVERSE RELAY SETTING CALCULATOR (WOULD-BE FAULT INPUT)
    # ---------------------------------------------------------
    st.markdown("#### 🧮 1. Prospective ('Would-Be') Fault Current ➔ Pickup & TMS Calculator")
    rc1, rc2, rc3, rc4 = st.columns(4)
    with rc1:
        calc_ct_pri = st.number_input("CT Primary Rating (A)", value=float(default_lv_ct / 2), step=50.0, key="calc_ct")
        calc_max_load = st.number_input("Max Full Load Current (A)", value=110.0, step=10.0, key="calc_fl")
        calc_ovl_factor = st.selectbox("Overload Pickup Factor (x I_load)", [1.20, 1.25, 1.30, 1.50], index=1)
    with rc2:
        calc_fault_3ph = st.number_input(
            "Prospective ('Would-Be') 3Ph Fault (A)",
            min_value=50.0, max_value=50000.0, value=float(round(ptr_data["max_3ph_sc_lv_A"] * 0.65, -1)), step=100.0
        )
        calc_target_t_oc = st.number_input("Desired Phase 51 Operating Time (s)", min_value=0.05, max_value=5.0, value=0.25, step=0.05, format="%.2f")
        calc_curve_str = st.selectbox("IDMT Curve Type", list(CURVE_CONSTANTS.keys()), index=0, key="calc_crv")
    with rc3:
        calc_fault_ef = st.number_input(
            "Prospective ('Would-Be') Earth Fault (A)",
            min_value=5.0, max_value=25000.0, value=float(round(ptr_data["max_ef_lv_A"] * 0.85, 0)), step=10.0
        )
        calc_target_t_ef = st.number_input("Desired Earth 51N Operating Time (s)", min_value=0.05, max_value=5.0, value=0.20, step=0.05, format="%.2f")
        calc_ef_sens_pct = st.selectbox("EF Pickup Sensitivity (% of CT)", [10.0, 15.0, 20.0, 30.0], index=1)

    c_calc = CURVE_CONSTANTS[calc_curve_str]
    rec_oc_pickup = round(calc_max_load * calc_ovl_factor, 1)
    rec_oc_plug_pct = round((rec_oc_pickup / calc_ct_pri) * 100.0, 1)
    oc_tms_res = calc_required_tms(calc_fault_3ph, rec_oc_pickup, calc_target_t_oc, c_calc)

    rec_ef_pickup = round(min(calc_ct_pri * (calc_ef_sens_pct / 100.0), ptr_data["max_ef_lv_A"] * 0.25), 1)
    rec_ef_plug_pct = round((rec_ef_pickup / calc_ct_pri) * 100.0, 1)
    ef_tms_res = calc_required_tms(calc_fault_ef, rec_ef_pickup, calc_target_t_ef, c_calc)

    with rc4:
        st.markdown("**🎯 Calculated Relay Settings**")
        if oc_tms_res:
            st.success(
                f"**Phase (ANSI 51):**\n"
                f"- Pickup ($I_p$): **{rec_oc_pickup} A** ({rec_oc_plug_pct}% of CT)\n"
                f"- PSM at {calc_fault_3ph:.0f}A: **{oc_tms_res['psm']}x**\n"
                f"- Required TMS: **{oc_tms_res['recommended_tms']}**\n"
                f"- Rec. ANSI 50 High-Set: **{calc_fault_3ph * 0.80:.0f} A**"
            )
        else:
            st.error("Prospective 3Ph Fault must exceed Phase Pickup Current.")

        if ef_tms_res:
            st.info(
                f"**Earth Fault (ANSI 51N):**\n"
                f"- Pickup ($I_{{p,EF}}$): **{rec_ef_pickup} A** ({rec_ef_plug_pct}% of CT)\n"
                f"- PSM at {calc_fault_ef:.0f}A: **{ef_tms_res['psm']}x**\n"
                f"- Required EF TMS: **{ef_tms_res['recommended_tms']}**"
            )

    st.divider()

    # ---------------------------------------------------------
    # SECTION B: ASSOCIATED 11kV FEEDERS FOR SELECTED SUBSTATION
    # ---------------------------------------------------------
    st.markdown(f"#### 🔌 2. Associated 11kV Feeders for `{active_ss_name}` (SS Code: `{active_ss_code}` | `{ptr_mva} MVA` PTR)")
    st.caption("Feeders and Feeder Codes are automatically populated for the selected Circle & Substation. You can edit any cell or add extra feeders below.")

    edited_feeders_df = st.data_editor(
        associated_11kv_df,
        num_rows="dynamic",
        use_container_width=True,
        key=f"feeder_editor_{selected_circle}_{active_ss_code}"
    )

    pcol1, pcol2, pcol3 = st.columns(3)
    with pcol1:
        sub_curve_name = st.selectbox("Substation Standard IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0)
        c_sub = CURVE_CONSTANTS[sub_curve_name]
        auto_grade_ptr = st.checkbox("Auto-Coordinate PTR LV & 33kV HV above Slowest Feeder", value=True)
    with pcol2:
        ptr_lv_ct = st.number_input("PTR 11kV LV CT Primary (A)", value=float(default_lv_ct), step=50.0, key="m_lv_ct")
        ptr_lv_oc_pickup = st.number_input("PTR 11kV LV 51 Pickup (A)", value=float(round(ptr_data["i_fl_lv_A"] * 1.15, -1)), step=10.0)
        ptr_lv_ef_pickup = st.number_input("PTR Neutral 51G Pickup (A)", value=float(round(ptr_data["rec_51g_pickup_A"], 0)), step=5.0)
    with pcol3:
        ptr_hv_ct = st.number_input("PTR 33kV HV CT Primary (A)", value=float(default_hv_ct), step=50.0, key="m_hv_ct")
        ptr_hv_oc_pickup_33kv = st.number_input("PTR 33kV HV 51 Pickup (A at 33kV)", value=float(round(ptr_data["i_fl_hv_A"] * 1.25, 0)), step=5.0)
        ptr_hv_50_pickup_33kv = st.number_input("PTR 33kV HV 50 High-Set (A at 33kV)", value=float(round(ptr_data["max_3ph_sc_hv_A"] * 1.25, -1)), step=50.0)

    max_fdr_idmt_time_at_bus = 0.0
    max_fdr_ef_time_at_ngr = 0.0
    slowest_feeder_name = "N/A"

    bus_fault_11kv = ptr_data["max_3ph_sc_lv_A"]
    ngr_fault_11kv = ptr_data["max_ef_lv_A"]

    for _, row in edited_feeders_df.iterrows():
        t_fdr_idmt = calc_idmt_time(bus_fault_11kv, float(row["51 Pickup (A)"]), float(row["51 TMS"]), c_sub)
        if t_fdr_idmt and t_fdr_idmt > max_fdr_idmt_time_at_bus:
            max_fdr_idmt_time_at_bus = t_fdr_idmt
            slowest_feeder_name = f"{row['11kV Feeder Name']} ({row['11kV Feeder Code']})"

        t_fdr_ef = calc_idmt_time(ngr_fault_11kv, float(row["51N Pickup (A)"]), float(row["51N TMS"]), c_sub)
        if t_fdr_ef and t_fdr_ef > max_fdr_ef_time_at_ngr:
            max_fdr_ef_time_at_ngr = t_fdr_ef

    if auto_grade_ptr and max_fdr_idmt_time_at_bus > 0:
        req_lv_time = max_fdr_idmt_time_at_bus + total_cti_sec
        lv_tms_calc = calc_required_tms(bus_fault_11kv, ptr_lv_oc_pickup, req_lv_time, c_sub)
        ptr_lv_oc_tms = lv_tms_calc["recommended_tms"] if lv_tms_calc else 0.18

        req_hv_time = req_lv_time + total_cti_sec
        hv_ref_pickup_11kv = ptr_hv_oc_pickup_33kv * ptr_data["turn_ratio"]
        hv_tms_calc = calc_required_tms(bus_fault_11kv, hv_ref_pickup_11kv, req_hv_time, c_sub)
        ptr_hv_oc_tms = hv_tms_calc["recommended_tms"] if hv_tms_calc else 0.25

        req_lv_ef_time = max_fdr_ef_time_at_ngr + total_cti_sec
        lv_ef_tms_calc = calc_required_tms(ngr_fault_11kv, ptr_lv_ef_pickup, req_lv_ef_time, c_sub)
        ptr_lv_ef_tms = lv_ef_tms_calc["recommended_tms"] if lv_ef_tms_calc else 0.15
    else:
        mcol1, mcol2, mcol3 = st.columns(3)
        ptr_lv_oc_tms = mcol1.number_input("Manual PTR LV 51 TMS", value=0.18, step=0.01)
        ptr_lv_ef_tms = mcol2.number_input("Manual PTR Neutral 51G TMS", value=0.14, step=0.01)
        ptr_hv_oc_tms = mcol3.number_input("Manual PTR HV 51 TMS", value=0.25, step=0.01)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Critical (Slowest) Feeder", slowest_feeder_name, f"Bus IDMT: {max_fdr_idmt_time_at_bus:.3f} s")
    k2.metric("Coordinated PTR LV 51 Setting", f"{ptr_lv_oc_pickup:.0f} A | TMS: {ptr_lv_oc_tms}", f"Plug: {(ptr_lv_oc_pickup/ptr_lv_ct)*100:.1f}%")
    k3.metric("Coordinated PTR Neutral 51G", f"{ptr_lv_ef_pickup:.0f} A | TMS: {ptr_lv_ef_tms}", f"NGR Max: {ngr_fault_11kv} A")
    k4.metric("Coordinated 33kV PTR HV 51", f"{ptr_hv_oc_pickup_33kv:.0f} A | TMS: {ptr_hv_oc_tms}", f"50 Inst: {ptr_hv_50_pickup_33kv:.0f} A (33kV)")

    # ---------------------------------------------------------
    # SECTION C: MULTI-FEEDER + PTR LV + PTR HV TCC PLOT
    # ---------------------------------------------------------
    fig_multi = make_subplots(
        rows=1, cols=2,
        subplot_titles=(
            f"{active_ss_name} Phase Coordination ({len(edited_feeders_df)} Feeders + PTR LV + 33kV HV)",
            f"{active_ss_name} Earth Fault Coordination (Feeders 51N vs PTR Neutral 51G)"
        )
    )

    max_3ph_int = int(bus_fault_11kv)
    max_ef_int = int(ngr_fault_11kv)
    fc_oc_list = list(range(20, int(max_3ph_int * 1.25) + 25, 25))
    step_ef_m = max(2, int(max_ef_int / 120))
    fc_ef_list = list(range(5, max_ef_int + step_ef_m, step_ef_m))

    for idx, row in edited_feeders_df.iterrows():
        color = FEEDER_COLORS[idx % len(FEEDER_COLORS)]
        f_label = f"{row['11kV Feeder Code']}: {row['11kV Feeder Name']}"
        fx_oc, fy_oc = build_curve_trace(
            fc_oc_list, float(row["51 Pickup (A)"]), float(row["51 TMS"]), c_sub,
            bool(row["50 Enabled"]), float(row["50 Pickup (A)"]), float(row["50 Delay (s)"])
        )
        fig_multi.add_trace(go.Scatter(x=fx_oc, y=fy_oc, name=f"{f_label} (51+50)", line=dict(color=color, width=2)), row=1, col=1)

        fx_ef, fy_ef = build_curve_trace(fc_ef_list, float(row["51N Pickup (A)"]), float(row["51N TMS"]), c_sub, False, None, 0.05)
        fig_multi.add_trace(go.Scatter(x=fx_ef, y=fy_ef, name=f"{f_label} (51N)", line=dict(color=color, width=2, dash="dot")), row=1, col=2)

    lv_x, lv_y = build_curve_trace(fc_oc_list, ptr_lv_oc_pickup, ptr_lv_oc_tms, c_sub, False, None, 0.2)
    hv_ref_11kv = ptr_hv_oc_pickup_33kv * ptr_data["turn_ratio"]
    hv_50_ref_11kv = ptr_hv_50_pickup_33kv * ptr_data["turn_ratio"]
    hv_x, hv_y = build_curve_trace(fc_oc_list, hv_ref_11kv, ptr_hv_oc_tms, c_sub, True, hv_50_ref_11kv, 0.05)

    fig_multi.add_trace(go.Scatter(x=lv_x, y=lv_y, name="PTR 11kV LV Incomer (51)", line=dict(color="#0288d1", width=3.5)), row=1, col=1)
    fig_multi.add_trace(go.Scatter(x=hv_x, y=hv_y, name=f"33kV {active_33kv_code} HV (51+50 Ref 11kV)", line=dict(color="#1b5e20", width=3.5, dash="dash")), row=1, col=1)
    fig_multi.add_vline(x=max_3ph_int, line_dash="solid", line_color="black", annotation_text=f"Max 11kV Bus SC: {max_3ph_int}A", row=1, col=1)

    lv_ef_x, lv_ef_y = build_curve_trace(fc_ef_list, ptr_lv_ef_pickup, ptr_lv_ef_tms, c_sub, False, None, 0.2)
    fig_multi.add_trace(go.Scatter(x=lv_ef_x, y=lv_ef_y, name="PTR Neutral (51G)", line=dict(color="#0288d1", width=3.5)), row=1, col=2)
    fig_multi.add_vline(x=max_ef_int, line_dash="solid", line_color="purple", annotation_text=f"NGR Max EF: {max_ef_int}A", row=1, col=2)

    fig_multi.update_xaxes(type="log", title_text="Fault Current Referred to 11kV Base (A)")
    fig_multi.update_yaxes(type="log", title_text="Operating Time (s)", range=[-1.6, 2.0])
    fig_multi.update_layout(height=560, legend=dict(orientation="h", yanchor="bottom", y=-0.30, xanchor="center", x=0.5))
    st.plotly_chart(fig_multi, use_container_width=True)

    # ---------------------------------------------------------
    # SECTION D: MASTER RELAY SCHEDULE & COORDINATION AUDIT TABLES
    # ---------------------------------------------------------
    st.markdown(f"#### 📋 3. Master Relay Setting Schedule & Coordination Audit — `{active_ss_name} ({active_ss_code})`")

    schedule_rows = []
    audit_multi_rows = []

    for _, row in edited_feeders_df.iterrows():
        f_code = str(row["11kV Feeder Code"])
        f_name = str(row["11kV Feeder Name"])
        f_ct = float(row["CT Primary (A)"])
        f_fl = float(row["Peak Load (A)"])
        f_sc = float(row["Prospective 3Ph Fault (A)"])
        f_51_p = float(row["51 Pickup (A)"])
        f_51_tms = float(row["51 TMS"])
        f_50_en = bool(row["50 Enabled"])
        f_50_p = float(row["50 Pickup (A)"])
        f_50_t = float(row["50 Delay (s)"])
        f_51n_p = float(row["51N Pickup (A)"])
        f_51n_tms = float(row["51N TMS"])

        schedule_rows.append({
            "Bay / Feeder Code": f_code,
            "Bay / Protection Zone Name": f_name,
            "Voltage": "11 kV",
            "CT Ratio": f"{int(f_ct)}/5 A",
            "Peak Load (A)": f_fl,
            "51 Pickup (A) / Plug (%)": f"{f_51_p:.0f} A ({(f_51_p/f_ct)*100:.1f}%)",
            "51 TMS": f_51_tms,
            "ANSI 50 High-Set": f"{f_50_p:.0f} A ({f_50_t}s)" if f_50_en else "Disabled",
            "51N/51G Pickup (A) / Plug (%)": f"{f_51n_p:.0f} A ({(f_51n_p/f_ct)*100:.1f}%)",
            "51N/51G TMS": f_51n_tms
        })

        t_fdr_idmt = calc_idmt_time(f_sc, f_51_p, f_51_tms, c_sub)
        t_fdr_comp = calc_composite_time(f_sc, f_51_p, f_51_tms, c_sub, f_50_en, f_50_p, f_50_t)
        t_lv_backup = calc_idmt_time(f_sc, ptr_lv_oc_pickup, ptr_lv_oc_tms, c_sub)
        t_hv_backup = calc_idmt_time(f_sc, hv_ref_11kv, ptr_hv_oc_tms, c_sub)

        margin_lv_fdr_ms = (t_lv_backup - t_fdr_comp) * 1000.0 if (t_lv_backup and t_fdr_comp) else None
        margin_hv_lv_ms = (t_hv_backup - t_lv_backup) * 1000.0 if (t_hv_backup and t_lv_backup) else None

        verdict = "✅ Selective"
        if (margin_lv_fdr_ms is not None and margin_lv_fdr_ms < total_cti_sec * 1000.0) or (margin_hv_lv_ms is not None and margin_hv_lv_ms < total_cti_sec * 1000.0):
            verdict = "⚠️ Margin < CTI"

        audit_multi_rows.append({
            "11kV Feeder Code": f_code,
            "11kV Feeder Name": f_name,
            "Prospective 3Ph Fault (A)": f_sc,
            "Feeder 51 IDMT Time (s)": round(t_fdr_idmt, 3) if t_fdr_idmt else "N/A",
            "Feeder Composite (51+50) Time (s)": round(t_fdr_comp, 3) if t_fdr_comp else "N/A",
            "PTR LV 51 Backup Time (s)": round(t_lv_backup, 3) if t_lv_backup else "N/A",
            "PTR HV 51 Backup Time (s)": round(t_hv_backup, 3) if t_hv_backup else "N/A",
            "Grading Margin LV-Feeder (ms)": round(margin_lv_fdr_ms, 1) if margin_lv_fdr_ms else "N/A",
            "Grading Margin HV-LV (ms)": round(margin_hv_lv_ms, 1) if margin_hv_lv_ms else "N/A",
            "Coordination Verdict": verdict
        })

    schedule_rows.append({
        "Bay / Feeder Code": f"{active_ss_code}-LV",
        "Bay / Protection Zone Name": f"PTR 11kV LV Incomer ({ptr_mva} MVA)",
        "Voltage": "11 kV",
        "CT Ratio": f"{int(ptr_lv_ct)}/5 A",
        "Peak Load (A)": ptr_data["i_fl_lv_A"],
        "51 Pickup (A) / Plug (%)": f"{ptr_lv_oc_pickup:.0f} A ({(ptr_lv_oc_pickup/ptr_lv_ct)*100:.1f}%)",
        "51 TMS": ptr_lv_oc_tms,
        "ANSI 50 High-Set": "Disabled (Time Graded)",
        "51N/51G Pickup (A) / Plug (%)": f"{ptr_lv_ef_pickup:.0f} A ({(ptr_lv_ef_pickup/ptr_lv_ct)*100:.1f}%)",
        "51N/51G TMS": ptr_lv_ef_tms
    })
    schedule_rows.append({
        "Bay / Feeder Code": active_33kv_code,
        "Bay / Protection Zone Name": f"PTR 33kV HV Primary ({active_33kv_fdr})",
        "Voltage": "33 kV",
        "CT Ratio": f"{int(ptr_hv_ct)}/5 A",
        "Peak Load (A)": ptr_data["i_fl_hv_A"],
        "51 Pickup (A) / Plug (%)": f"{ptr_hv_oc_pickup_33kv:.0f} A ({(ptr_hv_oc_pickup_33kv/ptr_hv_ct)*100:.1f}%)",
        "51 TMS": ptr_hv_oc_tms,
        "ANSI 50 High-Set": f"{ptr_hv_50_pickup_33kv:.0f} A at 33kV (0.05s)",
        "51N/51G Pickup (A) / Plug (%)": "Delta Winding (Isolated)",
        "51N/51G TMS": "N/A"
    })

    df_schedule = pd.DataFrame(schedule_rows)
    df_multi_audit = pd.DataFrame(audit_multi_rows)

    st.dataframe(df_schedule, use_container_width=True)
    st.dataframe(df_multi_audit, use_container_width=True)

    # ---------------------------------------------------------
    # SECTION E: DOWNLOAD REPORT (HTML/PDF & CSV)
    # ---------------------------------------------------------
    html_report = generate_html_commissioning_report(
        selected_circle, active_division, active_33kv_fdr, active_33kv_code,
        active_ss_name, active_ss_code, engineer_name,
        ptr_mva, ptr_z_pct, ngr_ohms, ptr_data,
        df_schedule, df_multi_audit, total_cti_sec * 1000.0
    )

    dl1, dl2, dl3 = st.columns(3)
    with dl1:
        st.download_button(
            label=f"📄 Download {active_ss_name} ({active_ss_code}) Relay Report (HTML/PDF)",
            data=html_report.encode("utf-8"),
            file_name=f"{selected_circle}_{active_ss_name}_{active_ss_code}_Relay_Report.html",
            mime="text/html"
        )
    with dl2:
        st.download_button(
            label="📥 Download Master Relay Setting Schedule (CSV)",
            data=df_schedule.to_csv(index=False).encode("utf-8"),
            file_name=f"{selected_circle}_{active_ss_name}_{active_ss_code}_Schedule.csv",
            mime="text/csv"
        )
    with dl3:
        st.download_button(
            label="📥 Download Multi-Feeder Coordination Audit (CSV)",
            data=df_multi_audit.to_csv(index=False).encode("utf-8"),
            file_name=f"{selected_circle}_{active_ss_name}_{active_ss_code}_Audit.csv",
            mime="text/csv"
        )
