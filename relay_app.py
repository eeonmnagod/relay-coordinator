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
# 1. GOOGLE SHEET BACKEND CONFIG & ROW-2 PARSER ENGINE
# ---------------------------------------------------------
DEFAULT_PEAK_LOAD_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/1Cr3bHdLWR53skYROA4ZgGMScCjm0QHMAuvHKdIrnLvk/edit?usp=drivesdk"
)
TARGET_OUTPUT_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/1gd11j6eF6Qx4pB2Ckdf9iF7DoeBCD15Cl179Z0VnIxs/edit?usp=sharing"
)
DEFAULT_GAS_WEBHOOK_URL = (
    "https://script.google.com/macros/s/AKfycbwyWsu97iR--0gOaTwrpELAtNW-6ZHK6Qf0CNFFlRWdsW0Ltn3FFf6Ac-f6CmbkFyN5/exec"
)

KNOWN_MPEZ_CIRCLE_TABS = [
    "CHHINDWARA", "SEONI", "BALAGHAT", "NARSINGHPUR", "JABALPUR CITY",
    "JABALPUR (O&M)", "KATNI", "MANDLA", "DINDORI", "REWA", "SATNA",
    "SIDHI", "SINGROLI", "SHAHDOL", "UMARIYA", "ANUPPUR", "SAGAR",
    "DAMOH", "CHHATARPUR", "TIKAMGARH", "PANNA"
]

CURVE_CONSTANTS = {
   # "3.0 Sec curve (IEC Standard Inverse)": 0.14,
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


def push_relay_schedule_to_division_tab(gas_url, payload):
    if not gas_url or not str(gas_url).startswith("https://script.google.com"):
        return {"status": "error", "message": "Please paste a valid Google Apps Script Web App URL in the sidebar."}
    try:
        resp = requests.post(gas_url.strip(), json=payload, headers={"Content-Type": "application/json"}, timeout=20)
        if resp.status_code == 200: return resp.json()
        return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text[:150]}"}
    except Exception as e: return {"status": "error", "message": str(e)}

def extract_sheet_id(sheet_url_or_id):
    m = re.search(r"/d/([a-zA-Z0-9-_]+)", str(sheet_url_or_id))
    return m.group(1) if m else str(sheet_url_or_id).strip()

def parse_ptr_mva_value(raw_val):
    if pd.isna(raw_val): return 5.0
    s = str(raw_val).upper().strip()
    m_to = re.search(r"(\d+(?:\.\d+)?)\s*TO\s*(\d+(?:\.\d+)?)", s)
    if m_to: return float(m_to.group(2))
    m_mult = re.search(r"\d+\s*[X*]\s*(\d+(?:\.\d+)?)", s)
    if m_mult: return float(m_mult.group(1))
    m_num = re.findall(r"\d+(?:\.\d+)?", s)
    if m_num:
        for val_str in reversed(m_num):
            v = float(val_str)
            if v in (1.6, 2.5, 3.15, 4.0, 5.0, 6.3, 8.0, 10.0, 12.5, 16.0, 20.0): return v
        v0 = float(m_num[0])
        if 1.0 <= v0 <= 50.0: return v0
    return 5.0

def recommend_standard_ct(load_or_fl_amps):
    standard_cts = [100, 150, 200, 300, 400, 600, 800, 1000, 1200]
    for ct in standard_cts:
        if ct >= load_or_fl_amps * 1.10: return float(ct)
    return 600.0

def parse_circle_tab_row2(df_raw, tab_name="UNKNOWN"):
    if df_raw is None or len(df_raw) < 3: return pd.DataFrame()
    circle_clean = tab_name.strip().upper()

    header_idx = 1
    for candidate_idx in [1, 0, 2, 3]:
        if candidate_idx < len(df_raw):
            row_str = " ".join([str(x).strip().lower() for x in df_raw.iloc[candidate_idx].values if pd.notna(x)])
            if sum(1 for k in ["division", "substation", "s/s", "feeder", "ptr", "mva", "capacity", "code"] if k in row_str) >= 2:
                header_idx = candidate_idx; break

    raw_cols = []
    for col_i in range(df_raw.shape[1]):
        h_main = str(df_raw.iloc[header_idx, col_i]).strip() if pd.notna(df_raw.iloc[header_idx, col_i]) else ""
        h_prev = str(df_raw.iloc[header_idx - 1, col_i]).strip() if (header_idx > 0 and pd.notna(df_raw.iloc[header_idx - 1, col_i])) else ""
        raw_cols.append(h_main if h_main.lower() not in ("nan", "none", "") else (h_prev if h_prev.lower() not in ("nan", "none", "") else f"col_{col_i}"))

    df = df_raw.iloc[header_idx + 1:].copy()
    seen = {}
    clean_cols = []
    for c in raw_cols:
        seen[c] = seen.get(c, 0) + 1
        clean_cols.append(f"{c}_{seen[c]}" if seen[c] > 1 else c)
    df.columns = clean_cols
    df = df.dropna(how="all")
    if df.empty: return pd.DataFrame()

    def sample_vals(col_name, n=25):
        return [str(x).strip() for x in df[col_name].dropna().head(n).tolist() if str(x).strip().lower() not in ("nan", "none", "")]

    def is_circle_column(col_name):
        vals = sample_vals(col_name)
        if not vals: return False
        matches = 0
        for v in vals:
            vu = v.upper()
            if "DIV" in vu or "DIVISION" in vu: return False
            if circle_clean in vu: matches += 1
        return (matches / len(vals)) > 0.65

    def is_valid_substation_column(col_name):
        if is_circle_column(col_name): return False
        vals = sample_vals(col_name)
        if not vals: return False
        a, n = 0, 0
        for v in vals:
            if re.match(r"^-?\d+(?:\.\d+)?$", v): n += 1
            elif re.search(r"[A-Za-z]", v): a += 1
        return False if (n / len(vals)) > 0.30 else (a / len(vals)) >= 0.50

    def is_valid_ptr_column(col_name):
        vals = sample_vals(col_name)
        if not vals: return False
        m_hits = 0
        for v in vals:
            vu = v.upper()
            if any(bad in vu for bad in ["VCB", "WORKING", "DEFECTIVE", "BYPASS", "MAKE", "REMARK"]): return False
            if re.search(r"\b(1\.6|2\.5|3\.15|4\.0|5|5\.0|6\.3|8|8\.0|10|10\.0|12\.5)\b", vu): m_hits += 1
        return (m_hits / len(vals)) >= 0.35

    used_cols = set()
    def pick_col(header_patterns, exclude_patterns=None, validator_fn=None):
        for pat in header_patterns:
            for col in df.columns:
                if col in used_cols: continue
                cl = re.sub(r"\s+", " ", col.lower()).strip()
                if any(ex in cl for ex in (exclude_patterns or [])): continue
                if re.search(pat, cl):
                    if validator_fn is None or validator_fn(col):
                        used_cols.add(col)
                        return col
        return None

    col_circle = pick_col([r"\bcircle\b", r"\bdistrict\b"], validator_fn=is_circle_column)
    if not col_circle:
        for col in df.columns:
            if is_circle_column(col): col_circle = col; used_cols.add(col); break
    
    col_div = pick_col([r"name\s*of.*division", r"name\s*of.*city.*division", r"city/o.?m.*division", r"\bdivision\b"], exclude_patterns=["substation", "s/s", "feeder", "code", "lat", "long", "sub-division", "sub division", "sub_division"])
    col_33code = pick_col([r"33\s*kv.*feeder.*code", r"33\s*kv.*code", r"33kv.*code", r"feeder code.*33"], exclude_patterns=["33/11", "s/s", "substation", "lat", "long"])
    col_33fdr = pick_col([r"name of.*33\s*kv.*feeder", r"33\s*kv.*feeder", r"33kv.*feeder", r"33\s*kv.*fdr", r"33kv.*fdr", r"name of 33"], exclude_patterns=["code", "33/11", "11kv", "11 kv", "s/s", "substation", "lat", "long"])
    col_ss = pick_col([r"name\s*of\s*associated\s*33\s*/\s*11\s*kv\s*s\s*/\s*s", r"associated\s*33\s*/\s*11\s*kv\s*s\s*/\s*s", r"name\s*of.*33\s*/\s*11\s*kv.*s\s*/\s*s", r"name\s*of.*33\s*/\s*11.*substation", r"33\s*/\s*11\s*kv.*s\s*/\s*s", r"33\s*/\s*11.*substation", r"name\s*of.*s\s*/\s*s", r"name\s*of.*substation", r"\bsubstation\b", r"\bs/s\b"], exclude_patterns=["code", "132", "33kv feeder", "33 kv feeder", "11kv feeder", "11 kv feeder", "11kv fdr", "11 kv fdr", "ptr", "capacity", "vcb", "lat", "long", "coord", "gps", "location"], validator_fn=is_valid_substation_column)
    col_sscode = pick_col([r"33\s*/\s*11.*s\s*/\s*s.*code", r"s\s*/\s*s.*code", r"ss.*code", r"substation.*code", r"sub-station.*code", r"33\s*/\s*11.*code"], exclude_patterns=["11kv feeder", "11 kv feeder", "33kv feeder", "33 kv feeder", "feeder", "fdr", "lat", "long"])
    col_ptr = pick_col([r"ptr.*cap", r"capacity", r"\bmva\b", r"ptr.*mva", r"transformer.*cap", r"\bptr\b"], exclude_patterns=["vcb", "status", "remark", "make", "code", "loading", "amp", "lat", "long"], validator_fn=is_valid_ptr_column)
    if not col_ptr:
        for col in df.columns:
            if col not in used_cols and is_valid_ptr_column(col): col_ptr = col; used_cols.add(col); break

    col_11code = pick_col([r"11\s*kv.*feeder.*code", r"11\s*kv.*code", r"11kv.*code", r"feeder.*code", r"fdr.*code", r"\bcode\b"], exclude_patterns=["33", "s/s", "ss", "substation", "div", "lat", "long"])
    col_11fdr = pick_col([r"name of.*11\s*kv.*feeder", r"11\s*kv.*feeder", r"11kv.*feeder", r"11\s*kv.*fdr", r"11kv.*fdr", r"outgoing.*feeder", r"feeder.*name", r"\bfeeder\b"], exclude_patterns=["33", "code", "s/s", "substation", "ptr", "vcb", "lat", "long"])
    col_load = pick_col([r"peak.*load", r"max.*load", r"load.*amp", r"current", r"\bamp\b", r"peak"], exclude_patterns=["vcb", "status", "remark", "code", "name", "mva", "lat", "long"])

    for p in [col_div, col_33fdr, col_33code, col_ss, col_sscode, col_ptr]:
        if p and p in df.columns: df[p] = df[p].ffill()

    records = []
    for idx, r in df.iterrows():
        ss_val = str(r[col_ss]).strip() if col_ss and pd.notna(r[col_ss]) else ""
        if not ss_val or ss_val.lower() in ("nan", "none", "total", "grand total", "s.no", "sr.no") or any(w in ss_val.lower() for w in ["name of", "substation", "33/11 kv", "s/s name", "latitude"]) or re.match(r"^-?\d+(?:\.\d+)?$", ss_val): continue
        div_val = str(r[col_div]).strip().upper() if col_div and pd.notna(r[col_div]) else f"{circle_clean} DIVISION"
        if div_val.lower() in ("nan", "none", ""): div_val = f"{circle_clean} DIVISION"
        f33_val = str(r[col_33fdr]).strip() if col_33fdr and pd.notna(r[col_33fdr]) else f"33kV {ss_val} Feeder"
        f33_code = str(r[col_33code]).strip().split(".")[0] if col_33code and pd.notna(r[col_33code]) else f"33F-{ss_val[:4].upper()}"
        sscode_val = str(r[col_sscode]).strip().split(".")[0] if col_sscode and pd.notna(r[col_sscode]) else f"SS-{idx}"
        ptr_raw = str(r[col_ptr]).strip() if col_ptr and pd.notna(r[col_ptr]) else "5.0 MVA"
        ptr_mva = parse_ptr_mva_value(ptr_raw)
        ptr_label = f"{ptr_mva:g} MVA ({ptr_raw})" if (ptr_raw and str(ptr_mva) not in ptr_raw and "vcb" not in ptr_raw.lower()) else f"{ptr_mva:g} MVA"
        f11_val, f11_code = str(r[col_11fdr]).strip() if col_11fdr and pd.notna(r[col_11fdr]) else "", str(r[col_11code]).strip().split(".")[0] if col_11code and pd.notna(r[col_11code]) else ""
        if f11_val.lower() in ("nan", "none"): f11_val = ""
        if f11_code.lower() in ("nan", "none"): f11_code = ""

        load_amps = 95.0
        if col_load and pd.notna(r[col_load]):
            m_ld = re.findall(r"\d+(?:\.\d+)?", str(r[col_load]))
            if m_ld:
                v_f = float(m_ld[0])
                if 1.0 <= v_f <= 1500.0: load_amps = v_f

        records.append({
            "circle": circle_clean, "division": div_val, "feeder_33kv": f33_val, "feeder_33kv_code": f33_code,
            "substation": ss_val.upper(), "ss_code": sscode_val, "ptr_label": ptr_label, "ptr_mva": ptr_mva,
            "feeder_11kv": f11_val, "feeder_11kv_code": f11_code, "peak_load_amps": load_amps
        })
    return pd.DataFrame(records)

def expand_missing_11kv_feeders(df_master):
    if df_master.empty: return df_master
    expanded_rows = []
    for (_, _, ss_name, _), grp in df_master.groupby(["circle", "division", "substation", "ptr_label"], sort=False):
        has_11kv = grp[grp["feeder_11kv_code"].astype(str).str.strip() != ""]
        if not has_11kv.empty:
            for _, row in grp.iterrows():
                r_dict = row.to_dict()
                if not r_dict["feeder_11kv_code"]: r_dict["feeder_11kv_code"] = f"{r_dict['ss_code']}-11F01"
                if not r_dict["feeder_11kv"]: r_dict["feeder_11kv"] = f"11kV {ss_name.title()} Feeder"
                expanded_rows.append(r_dict)
        else:
            base = grp.iloc[0].to_dict()
            i_fl = (float(base["ptr_mva"]) * 1e6) / (math.sqrt(3) * 11000.0)
            for sc, sn, sh in [("01", "Town", 0.34), ("02", "Industrial", 0.28), ("03", "Agriculture", 0.22), ("04", "Mixed", 0.16)]:
                rc = dict(base)
                rc["feeder_11kv_code"], rc["feeder_11kv"], rc["peak_load_amps"] = f"{base['ss_code']}-11F{sc}", f"11kV {ss_name.title()} {sn}", max(35.0, round(i_fl * sh, 0))
                expanded_rows.append(rc)
    return pd.DataFrame(expanded_rows)

@st.cache_data(ttl=600, show_spinner=False)
def load_company_peak_load_database(sheet_url):
    sheet_id = extract_sheet_id(sheet_url)
    all_dfs, loaded_tabs = [], []
    try:
        resp = requests.get(f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx", timeout=15)
        if resp.status_code == 200 and resp.content[:2] == b"PK":
            xls = pd.ExcelFile(io.BytesIO(resp.content))
            for tn in xls.sheet_names:
                if "wp" in tn.lower(): continue
                df_n = parse_circle_tab_row2(pd.read_excel(xls, sheet_name=tn, header=None), tab_name=tn)
                if not df_n.empty: all_dfs.append(df_n); loaded_tabs.append(tn)
    except: pass
    if not all_dfs:
        for tab in KNOWN_MPEZ_CIRCLE_TABS:
            try:
                r_csv = requests.get(f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(tab)}", timeout=6)
                if r_csv.status_code == 200 and "html" not in r_csv.text[:100].lower():
                    df_n = parse_circle_tab_row2(pd.read_csv(io.StringIO(r_csv.text), header=None), tab_name=tab)
                    if not df_n.empty: all_dfs.append(df_n); loaded_tabs.append(tab)
            except: continue
    if all_dfs: return expand_missing_11kv_feeders(pd.concat(all_dfs, ignore_index=True).drop_duplicates()), loaded_tabs, "LIVE_GSHEET"
    
    seeds = [("CHHINDWARA", "CHHINDWARA CITY", "33kV Pachmadhi Feeder", "33F-5155", "PACHMADHIIDHANA", "5155", "3.15 MVA (PTR-1)", 3.15), ("JABALPUR CITY", "EAST, JABALPUR", "33kV Gohalpur Feeder", "33F-1341", "GOHALPUR", "1341", "10 MVA (PTR-1)", 10.0)]
    return expand_missing_11kv_feeders(pd.DataFrame([{"circle": c, "division": d, "feeder_33kv": f, "feeder_33kv_code": fc, "substation": s, "ss_code": sc, "ptr_label": pl, "ptr_mva": m, "feeder_11kv": "", "feeder_11kv_code": "", "peak_load_amps": round(m*28.0, 1)} for c, d, f, fc, s, sc, pl, m in seeds])), KNOWN_MPEZ_CIRCLE_TABS, "FALLBACK"


# ---------------------------------------------------------
# 2. PROTECTION MATH & HIGH-SET COORDINATION FUNCTIONS
# ---------------------------------------------------------
def get_default_ptr_z_pct(ptr_mva):
    if ptr_mva <= 3.15: return 6.25
    elif ptr_mva <= 5.0: return 7.15
    elif ptr_mva <= 8.0: return 8.35
    return 9.00

def calc_idmt_time(fault_current, pickup_current, tms, curve_constant, alpha=0.02):
    if not pickup_current or pickup_current <= 0 or not tms or tms <= 0: return None
    psm = fault_current / pickup_current
    if psm <= 1.0: return None
    return (curve_constant / ((psm ** alpha) - 1.0)) * tms

def calc_required_tms(fault_current, pickup_current, target_time_sec, curve_constant, alpha=0.02, min_tms=0.025, max_tms=1.2):
    if not pickup_current or pickup_current <= 0 or fault_current <= pickup_current or target_time_sec <= 0: return None
    psm = fault_current / pickup_current
    raw_tms = (target_time_sec * ((psm ** alpha) - 1.0)) / curve_constant
    return {"psm": round(psm, 2), "raw_tms": round(raw_tms, 3), "recommended_tms": round(max(min_tms, min(max_tms, raw_tms)), 3)}

def calc_composite_time(fault_current, idmt_pickup, tms, curve_constant, hs_enabled=False, hs_pickup=None, hs_delay_sec=0.05):
    t_idmt = calc_idmt_time(fault_current, idmt_pickup, tms, curve_constant)
    if hs_enabled and hs_pickup and hs_pickup > 0 and fault_current >= hs_pickup:
        return min(t_idmt, hs_delay_sec) if t_idmt is not None else hs_delay_sec
    return t_idmt

def calculate_highset_for_margin_drop(up_pickup, up_tms, down_pickup, down_tms, curve_constant=0.14, target_margin_sec=0.100, max_fault_amps=20000):
    """Numerically solves for the current (A) where IDMT grading margin compresses below 100ms."""
    low, high = max(up_pickup, down_pickup) * 1.01, max_fault_amps
    m_high = calc_idmt_time(high, up_pickup, up_tms, curve_constant) - calc_idmt_time(high, down_pickup, down_tms, curve_constant)
    if m_high >= target_margin_sec: return None 
    for _ in range(50): 
        mid = (low + high) / 2.0
        margin = calc_idmt_time(mid, up_pickup, up_tms, curve_constant) - calc_idmt_time(mid, down_pickup, down_tms, curve_constant)
        if margin > target_margin_sec: low = mid
        else: high = mid
    return round(mid, 0)

def recommend_highsets(zone, ptr_data, feeder_reach_fault=0, fdr_50_delay=0.05, cti_sec=0.15):
    """Utility Logic Engine for Definite-Time High-Set Rules (ANSI 50/50N)"""
    if zone == "feeder":
        # Reach-based (typically 80% of line impedance)
        return {"50_pickup": round(feeder_reach_fault, -1), "50_delay": 0.05, "50N_pickup": round(feeder_reach_fault * 0.4, 0), "50N_delay": 0.05}
    elif zone == "ptr_lv":
        # DT Backup for bus faults. Must wait for feeder instantaneous to act + CTI.
        return {"50_pickup": round(ptr_data["max_3ph_sc_lv_A"] * 0.85, -1), "50_delay": fdr_50_delay + cti_sec, "50N_pickup": round(ptr_data["max_ef_lv_A"] * 0.90, 0), "50N_delay": fdr_50_delay + cti_sec}
    elif zone == "ptr_hv":
        # Pure Amplitude Graded above max 11kV side through-fault (referred to 33kV side)
        hv_referred_max = ptr_data["max_3ph_sc_hv_A"]
        return {"50_pickup": round(hv_referred_max * 1.25, -1), "50_delay": 0.05, "50N_pickup": None, "50N_delay": None} # Delta winding no 50N
    return {}

def calculate_ptr_parameters(v_hv_kv, v_lv_kv, ptr_mva, ptr_z_pct, ngr_ohms, ct_lv_pri, ct_hv_pri):
    turn_ratio = v_hv_kv / v_lv_kv
    v_lv_ln = (v_lv_kv * 1000.0) / math.sqrt(3)
    i_fl_lv, i_fl_hv = (ptr_mva * 1e6) / (math.sqrt(3) * v_lv_kv * 1000.0), (ptr_mva * 1e6) / (math.sqrt(3) * v_hv_kv * 1000.0)
    z_pu = ptr_z_pct / 100.0
    i_sc_3ph_lv, i_sc_3ph_hv = i_fl_lv / z_pu, i_fl_hv / z_pu
    z_ptr_ohms = ((v_lv_kv ** 2) / ptr_mva) * z_pu
    r_ptr_ohms = z_ptr_ohms / math.sqrt(1 + 10**2)
    x_ptr_ohms = r_ptr_ohms * 10.0
    dyn11_lg_hv_factor = 1.0 / (math.sqrt(3) * turn_ratio)

    max_ef_lv = (v_lv_ln / ngr_ohms) if ngr_ohms > 0 else i_sc_3ph_lv
    rec_51g_pickup = (max_ef_lv * 0.20) if ngr_ohms > 0 else (ct_lv_pri * 0.20)

    return {"turn_ratio": round(turn_ratio, 3), "v_lv_ln_volts": round(v_lv_ln, 1), "r_ptr_ohms": r_ptr_ohms, "x_ptr_ohms": x_ptr_ohms, "z_ptr_ohms": round(z_ptr_ohms, 4), "i_fl_lv_A": round(i_fl_lv, 2), "i_fl_hv_A": round(i_fl_hv, 2), "max_3ph_sc_lv_A": round(i_sc_3ph_lv, 1), "max_3ph_sc_hv_A": round(i_sc_3ph_hv, 1), "max_ef_lv_A": round(max_ef_lv, 2), "max_ef_reflected_hv_A": round(max_ef_lv * dyn11_lg_hv_factor, 2), "rec_51g_pickup_A": round(rec_51g_pickup, 2), "dyn11_lg_hv_factor": dyn11_lg_hv_factor}

def calculate_differential_settings(ptr_mva, v_hv, v_lv, hv_ct_pri, lv_ct_pri, ct_sec, ptr_z_pct):
    i_fl_hv, i_fl_lv = (ptr_mva * 1e6) / (math.sqrt(3) * v_hv * 1000.0), (ptr_mva * 1e6) / (math.sqrt(3) * v_lv * 1000.0)
    return {"i_fl_hv": round(i_fl_hv, 2), "i_fl_lv": round(i_fl_lv, 2), "i_sec_fl_hv": round(i_fl_hv / (hv_ct_pri / ct_sec), 3), "i_sec_fl_lv": round(i_fl_lv / (lv_ct_pri / ct_sec), 3), "max_through_fault_pu": round(1.0 / (ptr_z_pct / 100.0), 2), "idiff_pickup_pu": 0.20, "slope1_pct": 30, "slope2_pct": 80, "idiff_highset_pu": math.ceil(1.0 / (ptr_z_pct / 100.0) * 1.2), "harmonic_2nd_pct": 15, "harmonic_5th_pct": 35}

def calc_fault_at_distance_km(dist_km, v_ln_volts, r_ptr, x_ptr, r1_km, x1_km):
    z_total = math.hypot(r_ptr + (r1_km * dist_km), x_ptr + (x1_km * dist_km))
    return v_ln_volts / z_total if z_total > 0 else 0.0

def evaluate_ct_saturation(zone_name, ct_primary, ct_secondary, rated_va, ct_class_str, r_ct_ohms, lead_length_m, lead_sqmm, relay_va, max_fault_primary_amps):
    alf_rated = CT_CLASS_PRESETS.get(ct_class_str, 10.0)
    r_b_actual = ((2.0 * lead_length_m * 0.0178) / lead_sqmm if lead_sqmm > 0 else 0.0) + (relay_va / (ct_secondary ** 2))
    z_total_rated, z_total_actual = r_ct_ohms + (rated_va / (ct_secondary ** 2)), r_ct_ohms + r_b_actual
    alf_effective = alf_rated * (z_total_rated / z_total_actual) if z_total_actual > 0 else alf_rated
    saf_ratio = alf_effective / (max_fault_primary_amps / ct_primary if ct_primary > 0 else 0.0)
    return {"Zone": zone_name, "CT Ratio & Class": f"{int(ct_primary)}/{int(ct_secondary)}A ({rated_va:.0f}VA {ct_class_str})", "Rated Burden Z (Ω)": round(rated_va / (ct_secondary ** 2), 3), "Actual Loop Z (Rct+Lead+Relay Ω)": round(z_total_actual, 3), "Rated ALF": alf_rated, "Effective ALF (ALF_eff)": round(alf_effective, 1), "Max Unsat. Current (A)": round(alf_effective * ct_primary, 0), "Max Zone Fault (A)": round(max_fault_primary_amps, 0), "Actual Fault Multiple (x In)": round(max_fault_primary_amps / ct_primary if ct_primary > 0 else 0.0, 1), "CT Saturation Verdict": "✅ PASS (Linear / Unsaturated)" if saf_ratio >= 1.2 else ("⚠️ MARGINAL (<20% Headroom)" if saf_ratio >= 1.0 else "❌ SATURATES (Upgrade VA / Class)")}

def build_curve_trace(fc_list, idmt_pickup, tms, c_const, hs_en, hs_pickup, hs_delay):
    pts = list(fc_list)
    if pts and hs_en and hs_pickup and hs_pickup > pts[0] and hs_pickup <= pts[-1]:
        pts.extend([hs_pickup - 0.1, hs_pickup])
        pts = sorted(list(set(pts)))
    x_vals, y_vals = [], []
    for fc in pts:
        t = calc_composite_time(fc, idmt_pickup, tms, c_const, hs_en, hs_pickup, hs_delay)
        if t is not None: x_vals.append(fc); y_vals.append(t)
    return x_vals, y_vals


# ---------------------------------------------------------
# 3. STREAMLIT APP CONFIG & SIDEBAR
# ---------------------------------------------------------
st.set_page_config(page_title="MPPKVVCL Relay Coordination Suite", layout="wide")

with st.sidebar:
    st.title("⚡ MPPKVVCL Relay Suite")
    app_page = st.radio("Select Module Page:", ["Page 1: Single-Feeder Deep Dive", "Page 2: Multi-Feeder PTR Coordination & Report"], index=0)
    st.divider()
    
    st.header("1. Source Database")
    sheet_url_input = st.text_input("Source Peak Load Sheet URL", value=DEFAULT_PEAK_LOAD_SHEET_URL)
    uploaded_xlsx = st.file_uploader("Or Upload Local Peak Load Sheet (.xlsx/.csv)", type=["xlsx", "csv"])

    if uploaded_xlsx is not None:
        if uploaded_xlsx.name.endswith(".csv"):
            db_df, db_status = expand_missing_11kv_feeders(parse_circle_tab_row2(pd.read_csv(uploaded_xlsx, header=None), tab_name="UPLOADED")), "LOCAL_CSV"
        else:
            xls_up = pd.ExcelFile(uploaded_xlsx)
            up_dfs = [parse_circle_tab_row2(pd.read_excel(xls_up, sheet_name=tn, header=None), tab_name=tn) for tn in xls_up.sheet_names if "wp" not in tn.lower()]
            db_df, db_status = expand_missing_11kv_feeders(pd.concat([d for d in up_dfs if not d.empty], ignore_index=True)) if up_dfs else pd.DataFrame(), "LOCAL_XLSX"
    else:
        db_df, loaded_tabs, db_status = load_company_peak_load_database(sheet_url_input)

    st.success(f"✅ Database Ready ({len(db_df)} records)")
    st.divider()
    st.header("2. Target Output (Division Tab)")
    gas_webhook_url = st.text_input("Apps Script Web App URL", value=DEFAULT_GAS_WEBHOOK_URL)
    st.divider()
    
    st.header("3. Substation Constants")
    engineer_name = st.text_input("Engineer Name", value="Pallav Swarnkar")
    ngr_ohms = st.number_input("PTR NGR (Ω) [0 = Solid]", value=40.0, step=1.0)
    total_cti_sec = (st.number_input("VCB Open Time (ms)", value=60.0) + st.number_input("Relay Margin (ms)", value=40.0)) / 1000.0


# ---------------------------------------------------------
# 4. TOP CASCADING DROPDOWN BAR
# ---------------------------------------------------------
st.markdown("### 🏛️ Step-by-Step Substation, PTR & Feeder Selection")
dd1, dd2, dd3, dd4, dd5 = st.columns(5)
with dd1: selected_circle = st.selectbox("1️⃣ Circle", sorted(db_df["circle"].dropna().unique().tolist()))
df_cir = db_df[db_df["circle"] == selected_circle]
with dd2: selected_division = st.selectbox("2️⃣ Division", sorted(df_cir["division"].dropna().unique().tolist()))
df_div = df_cir[df_cir["division"] == selected_division]
with dd3: selected_substation = st.selectbox("3️⃣ Substation", sorted(df_div["substation"].dropna().unique().tolist()))
df_ss = df_div[df_div["substation"] == selected_substation]
with dd4: selected_ptr_label = st.selectbox("4️⃣ PTR Capacity", df_ss["ptr_label"].dropna().unique().tolist())
df_ptr = df_ss[df_ss["ptr_label"] == selected_ptr_label].reset_index(drop=True)
with dd5:
    feeder_options = [f"{row['feeder_11kv_code']} — {row['feeder_11kv']}" for _, row in df_ptr.iterrows()]
    selected_feeder_option = st.selectbox("5️⃣ 11kV Feeder", feeder_options)

active_row = df_ptr.iloc[feeder_options.index(selected_feeder_option)]
active_ss_name, active_ss_code, active_division = str(active_row["substation"]), str(active_row["ss_code"]), str(active_row["division"])
active_33kv_fdr, active_33kv_code = str(active_row["feeder_33kv"]), str(active_row["feeder_33kv_code"])
active_ptr_mva = float(active_row["ptr_mva"])
active_11kv_code, active_11kv_name, active_11kv_load = str(active_row["feeder_11kv_code"]), str(active_row["feeder_11kv"]), float(active_row["peak_load_amps"])

ptr_z_pct = get_default_ptr_z_pct(active_ptr_mva)
default_lv_ct, default_hv_ct, default_fdr_ct = recommend_standard_ct((active_ptr_mva * 1e6) / (math.sqrt(3) * 11000.0)), recommend_standard_ct((active_ptr_mva * 1e6) / (math.sqrt(3) * 33000.0)), recommend_standard_ct(active_11kv_load)
ptr_data = calculate_ptr_parameters(33.0, 11.0, active_ptr_mva, ptr_z_pct, ngr_ohms, default_lv_ct, default_hv_ct)

st.info(f"**Path:** {active_division} ➔ {active_33kv_fdr} ➔ **{active_ss_name}** ➔ {selected_ptr_label} ➔ **{active_11kv_code}** ({active_11kv_load}A)")


# =========================================================
# PAGE 1: SINGLE-FEEDER DEEP DIVE (REACH & CT SATURATION)
# =========================================================
if app_page.startswith("Page 1"):
    st.subheader(f"⚡ Page 1: Relay Settings for Feeder `{active_11kv_code}`")

    with st.sidebar:
        st.header("4. Line Specs")
        conductor_choice = st.selectbox("Conductor Type", list(CONDUCTOR_PRESETS.keys()), index=0)
        is_cust = conductor_choice == "Custom R + jX"
        r1_km = st.number_input("R1 (Ω/km)", value=CONDUCTOR_PRESETS[conductor_choice]["R1"], disabled=not is_cust)
        x1_km = st.number_input("X1 (Ω/km)", value=CONDUCTOR_PRESETS[conductor_choice]["X1"], disabled=not is_cust)
        feeder_len_km = st.number_input("Total Length (km)", value=12.0)
        t_reach_pct = st.slider("ANSI 50 Reach (%)", 50, 90, 80, 5)

        ct_sec_rat = st.selectbox("CT Sec (A)", [5.0, 1.0], index=0)
        l_sqmm = st.selectbox("Control Cable (sq.mm)", [2.5, 4.0, 6.0], index=0)
        l_len_m = st.number_input("One-Way Cable Length (m)", value=25.0)
        r_ct_wind = st.number_input("CT Rct (Ω)", value=0.25)
        rel_va = st.number_input("Relay Burden (VA)", value=0.50)

    # High-Set Calculation Core
    f_reach_amps = calc_fault_at_distance_km(feeder_len_km * (t_reach_pct / 100.0), ptr_data["v_lv_ln_volts"], ptr_data["r_ptr_ohms"], ptr_data["x_ptr_ohms"], r1_km, x1_km)
    hs_fdr = recommend_highsets("feeder", ptr_data, feeder_reach_fault=f_reach_amps)
    hs_lv = recommend_highsets("ptr_lv", ptr_data, cti_sec=total_cti_sec)
    hs_hv = recommend_highsets("ptr_hv", ptr_data)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(f"#### Zone 1: Feeder `{active_11kv_code}`")
        f_ct = st.number_input("1. CT Primary (A)", value=float(default_fdr_ct), step=50.0)
        c1a, c1b = st.columns(2)
        f_va, f_cls = c1a.selectbox("CT VA", [10.0, 15.0, 30.0], index=1), c1b.selectbox("CT Class", list(CT_CLASS_PRESETS.keys()), index=0)
        f_crv = st.selectbox("IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0, key="fc")
        f_51p = st.number_input("2. Phase 51 Pickup (A)", value=float(round(active_11kv_load * 1.25, 0)), step=5.0)
        f_51tms = st.number_input("3. Phase 51 TMS", value=0.08, format="%.2f")
        f_51np = st.number_input("4. Earth 51N Pickup (A)", value=float(round(f_ct * 0.15, 0)), step=5.0)
        f_51ntms = st.number_input("5. Earth 51N TMS", value=0.05, format="%.2f")
        
        st.markdown("**ANSI 50 High-Set (Auto-Recommended)**")
        f_50_en = st.checkbox("Enable Feeder 50 High-Set", value=True, key="f50en")
        f_50p = st.number_input("6. 50 Pickup (A) [80% Reach]", value=float(hs_fdr['50_pickup']), disabled=not f_50_en)
        f_50t = st.number_input("50 Delay (s)", value=float(hs_fdr['50_delay']), disabled=not f_50_en)
        f_50n_en = st.checkbox("Enable 50N EF High-Set", value=True, key="f50nen")
        f_50np = st.number_input("50N Pickup (A)", value=float(hs_fdr['50N_pickup']), disabled=not f_50n_en)
        f_50nt = st.number_input("50N Delay (s)", value=float(hs_fdr['50N_delay']), disabled=not f_50n_en)

    with col2:
        st.markdown(f"#### Zone 2: PTR LV Incomer")
        lv_ct = st.number_input("PTR LV CT (A)", value=float(default_lv_ct), step=50.0)
        c2a, c2b = st.columns(2)
        lv_va, lv_cls = c2a.selectbox("LV CT VA", [10.0, 15.0, 30.0], index=1), c2b.selectbox("LV CT Class", list(CT_CLASS_PRESETS.keys()), index=2)
        lv_crv = st.selectbox("LV IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0, key="lvc")
        lv_51p = st.number_input("LV 51 OC Pickup (A)", value=float(round(ptr_data["i_fl_lv_A"] * 1.15, -1)), step=10.0)
        lv_51tms = st.number_input("LV 51 OC TMS", value=0.15, format="%.2f")
        lv_51g = st.number_input("Neutral 51G Pickup (A)", value=float(round(ptr_data["rec_51g_pickup_A"], 0)), step=5.0)
        lv_51gtms = st.number_input("Neutral 51G TMS", value=0.12, format="%.2f")
        
        st.markdown("**ANSI 50 High-Set (Definite Time Backup)**")
        lv_50_en = st.checkbox("Enable LV 50", value=True, key="lv50en")
        lv_50p = st.number_input("LV 50 Pickup (A)", value=float(hs_lv['50_pickup']), disabled=not lv_50_en)
        lv_50t = st.number_input("LV 50 Delay (s) [CTI Graded]", value=float(hs_lv['50_delay']), disabled=not lv_50_en)
        lv_50g_en = st.checkbox("Enable 50G", value=True, key="lv50gen")
        lv_50gp = st.number_input("50G Pickup (A)", value=float(hs_lv['50N_pickup']), disabled=not lv_50g_en)
        lv_50gt = st.number_input("50G Delay (s) [CTI Graded]", value=float(hs_lv['50N_delay']), disabled=not lv_50g_en)

    with col3:
        st.markdown(f"#### Zone 3: 33kV PTR HV")
        hv_ct = st.number_input("PTR HV CT (A)", value=float(default_hv_ct), step=50.0)
        c3a, c3b = st.columns(2)
        hv_va, hv_cls = c3a.selectbox("HV CT VA", [10.0, 15.0, 30.0], index=1), c3b.selectbox("HV CT Class", list(CT_CLASS_PRESETS.keys()), index=2)
        hv_crv = st.selectbox("HV IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0, key="hvc")
        hv_51p = st.number_input("HV 51 OC Pickup (A@33kV)", value=float(round(ptr_data["i_fl_hv_A"] * 1.25, 0)), step=5.0)
        hv_51tms = st.number_input("HV 51 OC TMS", value=0.22, format="%.2f")
        
        st.markdown("**ANSI 50 High-Set (Stable for 11kV Faults)**")
        hv_50_en = st.checkbox("Enable HV 50 (33kV)", value=True, key="hv50en")
        hv_50p = st.number_input("HV 50 Pickup (A@33kV) [1.25x 11kV SC]", value=float(hs_hv['50_pickup']), disabled=not hv_50_en)
        hv_50t = st.number_input("HV 50 Delay (s)", value=float(hs_hv['50_delay']), disabled=not hv_50_en)

    # 87T Module for 10MVA+
    diff_en_p1 = False
    if active_ptr_mva >= 10.0:
        st.divider()
        st.subheader(f"🎛️ Transformer Differential (87T) for {active_ptr_mva} MVA PTR")
        diff_en_p1 = st.checkbox("Enable 87T Settings", value=True)
        if diff_en_p1:
            diff_data = calculate_differential_settings(active_ptr_mva, 33.0, 11.0, hv_ct, lv_ct, ct_sec_rat, ptr_z_pct)
            d_col1, d_col2, d_col3, d_col4 = st.columns(4)
            diff_pickup_p1 = d_col1.number_input("Id> Pickup (pu)", value=float(diff_data['idiff_pickup_pu']), step=0.05)
            diff_s1_p1 = d_col2.number_input("Bias Slope 1 (%)", value=float(diff_data['slope1_pct']), step=5.0)
            diff_s2_p1 = d_col3.number_input("Bias Slope 2 (%)", value=float(diff_data['slope2_pct']), step=5.0)
            diff_hs_p1 = d_col4.number_input("Id>> High-Set (pu)", value=float(diff_data['idiff_highset_pu']), step=1.0)
            st.info(f"**87T Compensation Check:** HV CT: {hv_ct}/{ct_sec_rat}A (Sec Fl: {diff_data['i_sec_fl_hv']}A) | LV CT: {lv_ct}/{ct_sec_rat}A (Sec Fl: {diff_data['i_sec_fl_lv']}A) | Max Through-Fault: {diff_data['max_through_fault_pu']} pu")

    st.divider()
    if st.button(f"☁️ Save Settings to `{active_division}` Tab", type="primary", use_container_width=True):
        p1_rows = [
            {"Bay / Feeder Code": active_11kv_code, "Bay / Protection Zone Name": active_11kv_name, "Voltage": "11 kV", "CT Ratio": f"{int(f_ct)}/{int(ct_sec_rat)} A", "Peak Load (A)": active_11kv_load, "51 Pickup (A) / Plug (%)": f"{f_51p:.0f} A ({(f_51p/f_ct)*100:.1f}%)", "51 TMS": f_51tms, "ANSI 50 High-Set": f"{f_50p:.0f} A ({f_50t}s)" if f_50_en else "Disabled", "51N/51G Pickup (A) / Plug (%)": f"{f_51np:.0f} A ({(f_51np/f_ct)*100:.1f}%)", "51N/51G TMS": f_51ntms},
            {"Bay / Feeder Code": f"{active_ss_code}-LV", "Bay / Protection Zone Name": f"PTR 11kV LV Incomer", "Voltage": "11 kV", "CT Ratio": f"{int(lv_ct)}/{int(ct_sec_rat)} A", "Peak Load (A)": ptr_data["i_fl_lv_A"], "51 Pickup (A) / Plug (%)": f"{lv_51p:.0f} A ({(lv_51p/lv_ct)*100:.1f}%)", "51 TMS": lv_51tms, "ANSI 50 High-Set": f"{lv_50p:.0f} A ({lv_50t}s)" if lv_50_en else "Disabled", "51N/51G Pickup (A) / Plug (%)": f"{lv_51g:.0f} A ({(lv_51g/lv_ct)*100:.1f}%)", "51N/51G TMS": lv_51gtms},
            {"Bay / Feeder Code": active_33kv_code, "Bay / Protection Zone Name": f"PTR 33kV HV Primary", "Voltage": "33 kV", "CT Ratio": f"{int(hv_ct)}/{int(ct_sec_rat)} A", "Peak Load (A)": ptr_data["i_fl_hv_A"], "51 Pickup (A) / Plug (%)": f"{hv_51p:.0f} A ({(hv_51p/hv_ct)*100:.1f}%)", "51 TMS": hv_51tms, "ANSI 50 High-Set": f"{hv_50p:.0f} A ({hv_50t}s)" if hv_50_en else "Disabled", "51N/51G Pickup (A) / Plug (%)": "Delta Winding", "51N/51G TMS": "N/A"}
        ]
        if diff_en_p1: p1_rows.append({"Bay / Feeder Code": f"{active_ss_code}-87T", "Bay / Protection Zone Name": f"PTR Differential (87T)", "Voltage": "33/11 kV", "CT Ratio": f"HV:{int(hv_ct)}/{int(ct_sec_rat)} | LV:{int(lv_ct)}/{int(ct_sec_rat)}", "Peak Load (A)": "N/A", "51 Pickup (A) / Plug (%)": f"Id> Pickup: {diff_pickup_p1} pu", "51 TMS": f"Slope 1: {diff_s1_p1}%", "ANSI 50 High-Set": f"Id>>: {diff_hs_p1} pu", "51N/51G Pickup (A) / Plug (%)": "2nd Harm: 15%", "51N/51G TMS": "5th Harm: 35%"})
        res = push_relay_schedule_to_division_tab(gas_webhook_url, {"engineer": engineer_name, "circle": selected_circle, "division": active_division, "feeder_33kv": active_33kv_fdr, "feeder_33kv_code": active_33kv_code, "substation": active_ss_name, "ss_code": active_ss_code, "ptr_label": selected_ptr_label, "ptr_z_pct": ptr_z_pct, "ngr_ohms": ngr_ohms, "cti_ms": total_cti_sec * 1000.0, "rows": p1_rows})
        st.success(f"✅ Saved to **[{res.get('tabName')}]**!") if res.get("status") == "success" else st.error(f"❌ Error: {res.get('message')}")

    # Plotting Logic
    fig = make_subplots(rows=1, cols=3, subplot_titles=("1. Phase OC TCC", "2. Earth Fault TCC", "3. Fault vs Distance"))
    c_fdr, c_lv, c_hv = CURVE_CONSTANTS[f_crv], CURVE_CONSTANTS[lv_crv], CURVE_CONSTANTS[hv_crv]
    
    fc_oc_range = list(range(20, int(ptr_data["max_3ph_sc_lv_A"] * 1.25) + 25, 25))
    fdr_oc_x, fdr_oc_y = build_curve_trace(fc_oc_range, f_51p, f_51tms, c_fdr, f_50_en, f_50p, f_50t)
    lv_oc_x, lv_oc_y = build_curve_trace(fc_oc_range, lv_51p, lv_51tms, c_lv, lv_50_en, lv_50p, lv_50t)
    hv_oc_x, hv_oc_y = build_curve_trace(fc_oc_range, hv_51p * ptr_data["turn_ratio"], hv_51tms, c_hv, hv_50_en, hv_50p * ptr_data["turn_ratio"], hv_50t)
    
    fig.add_trace(go.Scatter(x=fdr_oc_x, y=fdr_oc_y, name=f"Z1: {active_11kv_code}", line=dict(color="#d32f2f", width=2.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=lv_oc_x, y=lv_oc_y, name="Z2: PTR LV", line=dict(color="#0288d1", width=2.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=hv_oc_x, y=hv_oc_y, name="Z3: PTR HV", line=dict(color="#388e3c", width=2.5, dash="dash")), row=1, col=1)

    step_ef = max(2, int(ptr_data["max_ef_lv_A"] / 120))
    fc_ef_range = list(range(5, int(ptr_data["max_ef_lv_A"]) + step_ef, step_ef))
    fdr_ef_x, fdr_ef_y = build_curve_trace(fc_ef_range, f_51np, f_51ntms, c_fdr, f_50n_en, f_50np, f_50nt)
    lv_ef_x, lv_ef_y = build_curve_trace(fc_ef_range, lv_51g, lv_51gtms, c_lv, lv_50g_en, lv_50gp, lv_50gt)
    hv_lg_x, hv_lg_y = build_curve_trace(fc_ef_range, hv_51p * ptr_data["turn_ratio"] * math.sqrt(3), hv_51tms, c_hv, hv_50_en, hv_50p * ptr_data["turn_ratio"] * math.sqrt(3), hv_50t)
    
    fig.add_trace(go.Scatter(x=fdr_ef_x, y=fdr_ef_y, name=f"Z1 EF", line=dict(color="#d32f2f", width=2.5, dash="dot")), row=1, col=2)
    fig.add_trace(go.Scatter(x=lv_ef_x, y=lv_ef_y, name="Z2 EF", line=dict(color="#0288d1", width=2.5, dash="dot")), row=1, col=2)
    fig.add_trace(go.Scatter(x=hv_lg_x, y=hv_lg_y, name="Z3 EF", line=dict(color="#388e3c", width=2, dash="dashdot")), row=1, col=2)

    d_pts = [round(i * (feeder_len_km / 100.0), 2) for i in range(0, 101)]
    fig.add_trace(go.Scatter(x=d_pts, y=[calc_fault_at_distance_km(d, ptr_data["v_lv_ln_volts"], ptr_data["r_ptr_ohms"], ptr_data["x_ptr_ohms"], r1_km, x1_km) for d in d_pts], name="3Ph Fault", line=dict(color="#6a1b9a", width=3)), row=1, col=3)

    fig.update_xaxes(type="log", title_text="Fault Current (A)", row=1, col=1)
    fig.update_yaxes(type="log", title_text="Operating Time (s)", range=[-1.6, 2.0], row=1, col=1)
    fig.update_xaxes(type="log", title_text="Fault Current (A)", row=1, col=2)
    fig.update_yaxes(type="log", title_text="Operating Time (s)", range=[-1.6, 2.0], row=1, col=2)
    fig.update_xaxes(title_text="Feeder Distance (km)", row=1, col=3)
    fig.update_layout(height=480, legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5))
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("🧲 CT Saturation Verification")
    st.dataframe(pd.DataFrame([
        evaluate_ct_saturation(f"Z1: {active_11kv_code}", f_ct, ct_sec_rat, f_va, f_cls, r_ct_wind, l_len_m, l_sqmm, rel_va, ptr_data["max_3ph_sc_lv_A"]),
        evaluate_ct_saturation(f"Z2: PTR LV", lv_ct, ct_sec_rat, lv_va, lv_cls, r_ct_wind, l_len_m, l_sqmm, rel_va, ptr_data["max_3ph_sc_lv_A"]),
        evaluate_ct_saturation(f"Z3: PTR HV", hv_ct, ct_sec_rat, hv_va, hv_cls, r_ct_wind, l_len_m, l_sqmm, rel_va, ptr_data["max_3ph_sc_hv_A"])
    ]), use_container_width=True)


# =========================================================
# PAGE 2: MULTI-FEEDER PTR COORDINATION, AUTO-TMS & REPORT
# =========================================================
else:
    st.subheader(f"🏢 Page 2: Multi-Feeder Coordination for `{active_ss_name}`")
    ptr_state_key = f"p2_{selected_circle}_{active_division}_{active_ss_code}_{selected_ptr_label}"
    
    if ptr_state_key not in st.session_state:
        st.session_state[ptr_state_key] = pd.DataFrame([{
            "11kV Feeder Code": str(r["feeder_11kv_code"]), "11kV Feeder Name": str(r["feeder_11kv"]),
            "CT Primary (A)": int(recommend_standard_ct(r["peak_load_amps"] if r["peak_load_amps"] > 0 else 95.0)),
            "Peak Load (A)": round(r["peak_load_amps"] if r["peak_load_amps"] > 0 else 95.0, 1),
            "Prospective 3Ph Fault (A)": round(ptr_data["max_3ph_sc_lv_A"] * 0.65, -1),
            "51 Pickup (A)": round((r["peak_load_amps"] if r["peak_load_amps"] > 0 else 95.0) * 1.25, 0), "51 TMS": 0.08,
            "50 Enabled": True, "50 Pickup (A)": round(ptr_data["max_3ph_sc_lv_A"] * 0.52, -1), "50 Delay (s)": 0.05,
            "51N Pickup (A)": round(recommend_standard_ct(r["peak_load_amps"] if r["peak_load_amps"] > 0 else 95.0) * 0.15, 0), "51N TMS": 0.05,
            "50N Enabled": True, "50N Pickup (A)": round(recommend_standard_ct(r["peak_load_amps"] if r["peak_load_amps"] > 0 else 95.0) * 0.15 * 4, 0), "50N Delay (s)": 0.05
        } for _, r in df_ptr.iterrows()])

    st.markdown(f"#### 🎯 1. Auto-Calculate Phase & Earth TMS for Feeder `{active_11kv_code}`")
    fc1, fc2, fc3, fc4 = st.columns(4)
    with fc1:
        in_ct_pri = st.number_input("CT Primary (A)", value=float(default_fdr_ct), step=50.0)
        in_max_load = st.number_input("Peak Load (A)", value=float(active_11kv_load))
        in_ovl = st.selectbox("Overload Factor", [1.15, 1.20, 1.25, 1.30, 1.50], index=2)
    with fc2:
        in_f3 = st.number_input("3Ph Fault Current (A)", value=float(round(ptr_data["max_3ph_sc_lv_A"] * 0.65, -1)), step=100.0)
        in_toc = st.number_input("Target 51 Trip Time (s)", value=0.22, step=0.02)
        in_crv = st.selectbox("IDMT Curve", list(CURVE_CONSTANTS.keys()), index=0)
    
    auto_51p = round(in_max_load * in_ovl, 0)
    oc_tms = calc_required_tms(in_f3, auto_51p, in_toc, CURVE_CONSTANTS[in_crv])
    
    with fc3:
        i51p = st.number_input("3. Phase 51 Pickup (A)", value=float(auto_51p))
        i51t = st.number_input("4. Phase 51 TMS (Auto)", value=float(oc_tms["recommended_tms"] if oc_tms else 0.08), format="%.3f")
        i50p = st.number_input("5. 50 High-Set (A)", value=float(round(in_f3 * 0.80, -1)))
    
    with fc4:
        in_fef = st.number_input("Earth Fault Current (A)", value=float(round(ptr_data["max_ef_lv_A"] * 0.85, 0)))
        i51np = st.number_input("6. Earth 51N Pickup (A)", value=float(round(in_ct_pri * 0.15, 0)))
        ef_tms = calc_required_tms(in_fef, i51np, 0.18, CURVE_CONSTANTS[in_crv])
        i51nt = st.number_input("7. Earth 51N TMS", value=float(ef_tms["recommended_tms"] if ef_tms else 0.05), format="%.3f")
        
        if st.button(f"✅ Apply to {active_11kv_code}", use_container_width=True):
            df_c = st.session_state[ptr_state_key].copy()
            m = df_c["11kV Feeder Code"] == active_11kv_code
            if m.any():
                df_c.loc[m, ["CT Primary (A)", "Peak Load (A)", "Prospective 3Ph Fault (A)", "51 Pickup (A)", "51 TMS", "50 Pickup (A)", "51N Pickup (A)", "51N TMS"]] = [in_ct_pri, in_max_load, in_f3, i51p, i51t, i50p, i51np, i51nt]
                st.session_state[ptr_state_key] = df_c

    st.markdown(f"#### 🔌 2. Associated 11kV Feeders under `{active_ss_name}`")
    edited_fdf = st.data_editor(st.session_state[ptr_state_key], num_rows="dynamic", use_container_width=True)

    pcol1, pcol2, pcol3 = st.columns(3)
    with pcol1:
        sub_crv = st.selectbox("Substation Curve", list(CURVE_CONSTANTS.keys()), index=0)
        c_sub = CURVE_CONSTANTS[sub_crv]
        auto_grade = st.checkbox("Auto-Coordinate PTR LV & HV", value=True)
    with pcol2:
        ptr_lv_ct = st.number_input("PTR LV CT (A)", value=float(default_lv_ct), step=50.0)
        ptr_lv_51p = st.number_input("PTR LV 51 Pickup (A)", value=float(round(ptr_data["i_fl_lv_A"] * 1.15, -1)))
        ptr_lv_51g = st.number_input("PTR 51G Pickup (A)", value=float(round(ptr_data["rec_51g_pickup_A"], 0)))
    with pcol3:
        ptr_hv_ct = st.number_input("PTR HV CT (A)", value=float(default_hv_ct), step=50.0)
        ptr_hv_51p = st.number_input("PTR HV 51 Pickup (A@33kV)", value=float(round(ptr_data["i_fl_hv_A"] * 1.25, 0)))
        
        # PTR HV 50 Recommendation Logic
        hs_hv_p2 = recommend_highsets("ptr_hv", ptr_data)
        ptr_hv_50_en = st.checkbox("Enable PTR HV 50 (33kV)", value=True)
        ptr_hv_50p = st.number_input("PTR HV 50 High-Set (A@33kV)", value=float(hs_hv_p2['50_pickup']), disabled=not ptr_hv_50_en)
        ptr_hv_50t = st.number_input("PTR HV 50 Delay (s)", value=float(hs_hv_p2['50_delay']), disabled=not ptr_hv_50_en)

    max_t_oc, max_t_ef, slow_fdr = 0.0, 0.0, "N/A"
    for _, row in edited_fdf.iterrows():
        t_oc = calc_idmt_time(ptr_data["max_3ph_sc_lv_A"], float(row["51 Pickup (A)"]), float(row["51 TMS"]), c_sub)
        t_ef = calc_idmt_time(ptr_data["max_ef_lv_A"], float(row["51N Pickup (A)"]), float(row["51N TMS"]), c_sub)
        if t_oc and t_oc > max_t_oc: max_t_oc, slow_fdr = t_oc, row['11kV Feeder Code']
        if t_ef and t_ef > max_t_ef: max_t_ef = t_ef

    if auto_grade and max_t_oc > 0:
        lv_tms_c = calc_required_tms(ptr_data["max_3ph_sc_lv_A"], ptr_lv_51p, max_t_oc + total_cti_sec, c_sub)
        ptr_lv_tms = lv_tms_c["recommended_tms"] if lv_tms_c else 0.18
        hv_tms_c = calc_required_tms(ptr_data["max_3ph_sc_lv_A"], ptr_hv_51p * ptr_data["turn_ratio"], max_t_oc + (2 * total_cti_sec), c_sub)
        ptr_hv_tms = hv_tms_c["recommended_tms"] if hv_tms_c else 0.25
        lv_ef_c = calc_required_tms(ptr_data["max_ef_lv_A"], ptr_lv_51g, max_t_ef + total_cti_sec, c_sub)
        ptr_lv_eftms = lv_ef_c["recommended_tms"] if lv_ef_c else 0.15
        
        # Determine Exact Margin Drop Current for PTR LV High Set
        fdr_oc_p_slow = edited_fdf.loc[edited_fdf["11kV Feeder Code"] == slow_fdr, "51 Pickup (A)"].values[0]
        fdr_oc_t_slow = edited_fdf.loc[edited_fdf["11kV Feeder Code"] == slow_fdr, "51 TMS"].values[0]
        
        drop_curr = calculate_highset_for_margin_drop(ptr_lv_51p, ptr_lv_tms, fdr_oc_p_slow, fdr_oc_t_slow, c_sub, total_cti_sec)
    else:
        ptr_lv_tms, ptr_lv_eftms, ptr_hv_tms = 0.18, 0.14, 0.25
        drop_curr = None

    # PTR LV 50 Recommendation Logic
    st.markdown("##### PTR LV High-Set Backup")
    lv_50_c1, lv_50_c2, lv_50_c3, lv_50_c4 = st.columns(4)
    ptr_lv_50_en = lv_50_c1.checkbox("Enable PTR LV 50", value=True)
    
    # Calculate recommended LV high-set pickup. 
    # Use the drop_curr (if calculated) or just 85% of bus fault.
    rec_lv_50_p = drop_curr if drop_curr else round(ptr_data["max_3ph_sc_lv_A"] * 0.85, -1)
    
    ptr_lv_50p = lv_50_c2.number_input("PTR LV 50 (A)", value=float(rec_lv_50_p), disabled=not ptr_lv_50_en)
    ptr_lv_50t = lv_50_c3.number_input("PTR LV 50 Delay (s)", value=float(0.05 + total_cti_sec), disabled=not ptr_lv_50_en)
    if drop_curr: lv_50_c4.info(f"Grading < {total_cti_sec*1000}ms at {drop_curr}A")

    if active_ptr_mva >= 10.0:
        st.markdown("#### 🎛️ Transformer Differential Protection (87T)")
        diff_en_p2 = st.checkbox("Include 87T Settings in Schedule", value=True)
        if diff_en_p2:
            diff_data_p2 = calculate_differential_settings(active_ptr_mva, 33.0, 11.0, ptr_hv_ct, ptr_lv_ct, 5.0, ptr_z_pct)
            d2_1, d2_2, d2_3, d2_4 = st.columns(4)
            p2_diff_pickup = d2_1.number_input("Id> Pickup (pu)", value=float(diff_data_p2['idiff_pickup_pu']), step=0.05, key="p2_id")
            p2_diff_s1 = d2_2.number_input("Bias Slope 1 (%)", value=float(diff_data_p2['slope1_pct']), step=5.0, key="p2_s1")
            p2_diff_s2 = d2_3.number_input("Bias Slope 2 (%)", value=float(diff_data_p2['slope2_pct']), step=5.0, key="p2_s2")
            p2_diff_hs = d2_4.number_input("Id>> High-Set (pu)", value=float(diff_data_p2['idiff_highset_pu']), step=1.0, key="p2_hs")
    else: diff_en_p2 = False

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Slowest Feeder", slow_fdr, f"Bus Time: {max_t_oc:.3f}s")
    k2.metric("PTR LV TMS", f"{ptr_lv_tms}", f"Pickup: {ptr_lv_51p} A")
    k3.metric("PTR Neutral TMS", f"{ptr_lv_eftms}", f"Pickup: {ptr_lv_51g} A")
    k4.metric("PTR HV TMS", f"{ptr_hv_tms}", f"Pickup: {ptr_hv_51p} A (33kV)")

    st.markdown(f"#### 📋 3. Master Schedule & Grading Audit")
    sch_rows, aud_rows = [], []
    for _, r in edited_fdf.iterrows():
        f_ct = float(r["CT Primary (A)"])
        sch_rows.append({"Circle / Division": f"{selected_circle} / {active_division}", "Substation & PTR": f"{active_ss_name} ({selected_ptr_label})", "Bay / Feeder Code": r["11kV Feeder Code"], "Bay / Protection Zone Name": r["11kV Feeder Name"], "Voltage": "11 kV", "CT Ratio": f"{int(f_ct)}/5 A", "Peak Load (A)": r["Peak Load (A)"], "51 Pickup (A) / Plug (%)": f"{r['51 Pickup (A)']:.0f} A ({(r['51 Pickup (A)']/f_ct)*100:.1f}%)", "51 TMS": r["51 TMS"], "ANSI 50 High-Set": f"{r['50 Pickup (A)']:.0f} A ({r['50 Delay (s)']}s)" if r["50 Enabled"] else "Disabled", "51N/51G Pickup (A) / Plug (%)": f"{r['51N Pickup (A)']:.0f} A ({(r['51N Pickup (A)']/f_ct)*100:.1f}%)", "51N/51G TMS": r["51N TMS"]})
        
        t_f = calc_composite_time(r["Prospective 3Ph Fault (A)"], r["51 Pickup (A)"], r["51 TMS"], c_sub, r["50 Enabled"], r["50 Pickup (A)"], r["50 Delay (s)"])
        t_lv = calc_composite_time(r["Prospective 3Ph Fault (A)"], ptr_lv_51p, ptr_lv_tms, c_sub, ptr_lv_50_en, ptr_lv_50p, ptr_lv_50t)
        t_hv = calc_composite_time(r["Prospective 3Ph Fault (A)"], ptr_hv_51p * ptr_data["turn_ratio"], ptr_hv_tms, c_sub, ptr_hv_50_en, ptr_hv_50p * ptr_data["turn_ratio"], ptr_hv_50t)
        
        aud_rows.append({"11kV Feeder Code": r["11kV Feeder Code"], "Prospective 3Ph Fault (A)": r["Prospective 3Ph Fault (A)"], "Feeder Comp Time (s)": round(t_f, 3) if t_f else "N/A", "PTR LV Backup (s)": round(t_lv, 3) if t_lv else "N/A", "PTR HV Backup (s)": round(t_hv, 3) if t_hv else "N/A", "Grading Margin LV-Fdr (ms)": round((t_lv - t_f)*1000, 1) if (t_lv and t_f) else "N/A"})

    sch_rows.append({"Circle / Division": f"{selected_circle} / {active_division}", "Substation & PTR": f"{active_ss_name} ({selected_ptr_label})", "Bay / Feeder Code": f"{active_ss_code}-LV", "Bay / Protection Zone Name": f"PTR 11kV LV Incomer", "Voltage": "11 kV", "CT Ratio": f"{int(ptr_lv_ct)}/5 A", "Peak Load (A)": ptr_data["i_fl_lv_A"], "51 Pickup (A) / Plug (%)": f"{ptr_lv_51p:.0f} A ({(ptr_lv_51p/ptr_lv_ct)*100:.1f}%)", "51 TMS": ptr_lv_tms, "ANSI 50 High-Set": f"{ptr_lv_50p:.0f} A ({ptr_lv_50t}s)" if ptr_lv_50_en else "Disabled", "51N/51G Pickup (A) / Plug (%)": f"{ptr_lv_51g:.0f} A ({(ptr_lv_51g/ptr_lv_ct)*100:.1f}%)", "51N/51G TMS": ptr_lv_eftms})
    sch_rows.append({"Circle / Division": f"{selected_circle} / {active_division}", "Substation & PTR": f"{active_ss_name} ({selected_ptr_label})", "Bay / Feeder Code": active_33kv_code, "Bay / Protection Zone Name": f"PTR 33kV HV Primary", "Voltage": "33 kV", "CT Ratio": f"{int(ptr_hv_ct)}/5 A", "Peak Load (A)": ptr_data["i_fl_hv_A"], "51 Pickup (A) / Plug (%)": f"{ptr_hv_51p:.0f} A ({(ptr_hv_51p/ptr_hv_ct)*100:.1f}%)", "51 TMS": ptr_hv_tms, "ANSI 50 High-Set": f"{ptr_hv_50p:.0f} A @ 33kV ({ptr_hv_50t}s)" if ptr_hv_50_en else "Disabled", "51N/51G Pickup (A) / Plug (%)": "Isolated", "51N/51G TMS": "N/A"})

    if diff_en_p2:
        sch_rows.append({"Circle / Division": f"{selected_circle} / {active_division}", "Substation & PTR": f"{active_ss_name} ({selected_ptr_label})", "Bay / Feeder Code": f"{active_ss_code}-87T", "Bay / Protection Zone Name": f"PTR Differential (87T)", "Voltage": "33/11 kV", "CT Ratio": f"HV:{int(ptr_hv_ct)}/5 | LV:{int(ptr_lv_ct)}/5", "Peak Load (A)": "N/A", "51 Pickup (A) / Plug (%)": f"Id> Pickup: {p2_diff_pickup} pu", "51 TMS": f"Slope 1: {p2_diff_s1}%", "ANSI 50 High-Set": f"Id>>: {p2_diff_hs} pu", "51N/51G Pickup (A) / Plug (%)": "2nd Harm: 15%", "51N/51G TMS": "5th Harm: 35%"})

    df_sch = pd.DataFrame(sch_rows)
    df_aud = pd.DataFrame(aud_rows)
    st.dataframe(df_sch, use_container_width=True)

    if st.button(f"☁️ Save Schedule to `{active_division}` Tab", type="primary", use_container_width=True):
        res = push_relay_schedule_to_division_tab(gas_webhook_url, {"engineer": engineer_name, "circle": selected_circle, "division": active_division, "feeder_33kv": active_33kv_fdr, "feeder_33kv_code": active_33kv_code, "substation": active_ss_name, "ss_code": active_ss_code, "ptr_label": selected_ptr_label, "ptr_z_pct": ptr_z_pct, "ngr_ohms": ngr_ohms, "cti_ms": total_cti_sec * 1000.0, "rows": sch_rows})
        st.success(f"✅ Saved to **[{res.get('tabName')}]**!") if res.get("status") == "success" else st.error(f"❌ Error: {res.get('message')}")
