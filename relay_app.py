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
# 1. GOOGLE SHEET BACKEND CONFIG & CONDITIONAL FORM PARSER
# ---------------------------------------------------------
DEFAULT_PEAK_LOAD_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/1jzdbWPRC-zKd7qeQ9d5_eimUjqwYd5LbtCtTxmuhmOQ/edit?usp=sharing"
)

TARGET_OUTPUT_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/1gd11j6eF6Qx4pB2Ckdf9iF7DoeBCD15Cl179Z0VnIxs/edit?usp=sharing"
)

DEFAULT_GAS_WEBHOOK_URL = (
    "https://script.google.com/macros/s/AKfycbwyWsu97iR--0gOaTwrpELAtNW-6ZHK6Qf0CNFFlRWdsW0Ltn3FFf6Ac-f6CmbkFyN5/exec"
)

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
    m = re.search(r"/d/([a-zA-Z0-9-_]+)", str(sheet_url_or_id))
    return m.group(1) if m else str(sheet_url_or_id).strip()


def clean_cell_str(val):
    if pd.isna(val):
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none", "null", "na", "n/a", "-") else s


def extract_first_number(val, default=0.0):
    s = clean_cell_str(val)
    if not s:
        return default
    m = re.findall(r"\d+(?:\.\d+)?", s)
    return float(m[0]) if m else default


def coalesce_row_by_keywords(row, col_names, keywords, exclude=None):
    """
    Scans all conditional section columns matching `keywords` in a Form Response row
    and returns the first non-blank answer while skipping empty conditional branches.
    """
    exclude = exclude or []
    for col in col_names:
        cl = col.lower()
        if any(ex in cl for ex in exclude):
            continue
        if any(k in cl for k in keywords):
            val = clean_cell_str(row[col])
            if val:
                return val
    return ""


def parse_conditional_form_responses(df_form_raw):
    """
    Parses a Google Form Responses tab with conditional division-based sections:
    - Coalesces duplicated/branched section columns per row
    - Extracts the exact Division name that has filled data
    - Skips all blank columns and empty conditional sections
    """
    if df_form_raw is None or df_form_raw.empty:
        return pd.DataFrame()

    # Drop completely empty columns and completely empty rows first
    df = df_form_raw.dropna(axis=1, how="all").dropna(axis=0, how="all").copy()
    if df.empty:
        return pd.DataFrame()

    # Ensure unique column names if Google Forms created duplicate question headers across sections
    seen = {}
    unique_cols = []
    for c in df.columns:
        c_str = str(c).strip()
        seen[c_str] = seen.get(c_str, 0) + 1
        unique_cols.append(f"{c_str}__{seen[c_str]}" if seen[c_str] > 1 else c_str)
    df.columns = unique_cols

    parsed_rows = []
    for _, row in df.iterrows():
        # Strip base header name before '__' when matching keywords
        def find_field(keywords, exclude=None):
            exclude = exclude or []
            for col in df.columns:
                base_col = col.split("__")[0].lower()
                if any(ex in base_col for ex in exclude):
                    continue
                if any(k in base_col for k in keywords):
                    val = clean_cell_str(row[col])
                    if val:
                        return val
            return ""

        division = find_field(["division", "name of division", "div", "dn"], exclude=["sub", "code"])
        circle = find_field(["circle", "district"], exclude=["sub"])
        substation = find_field(["substation", "sub-station", "s/s", "33/11"], exclude=["code", "feeder", "33kv feeder"])
        ss_code = find_field(["ss code", "s/s code", "substation code"])
        feeder_33kv = find_field(["33kv feeder", "33 kv feeder", "source 33", "33kv"], exclude=["code", "s/s", "substation"])
        ptr_raw = find_field(["ptr", "capacity", "mva", "transformer"], exclude=["ct", "pt", "ratio", "code"])
        feeder_11kv = find_field(["11kv feeder", "11 kv feeder", "feeder name", "outgoing feeder", "bay"], exclude=["33", "code"])
        feeder_code = find_field(["feeder code", "11kv code", "11 kv code", "bay code"], exclude=["33", "ss", "s/s"])
        ct_ratio = find_field(["ct ratio", "ctr", "ct primary", "connected ct"])
        oc_pickup = find_field(["51 pickup", "oc pickup", "o/c pickup", "overcurrent pickup", "pickup (a)", "o/c"])
        oc_tms = find_field(["51 tms", "oc tms", "o/c tms", "tms"], exclude=["51n", "51g", "ef", "earth"])
        hs_50 = find_field(["50", "high set", "high-set", "instantaneous", "def"], exclude=["50n", "50g", "earth"])
        ef_pickup = find_field(["51n", "51g", "ef pickup", "earth fault pickup", "e/f"])
        ef_tms = find_field(["51n tms", "51g tms", "ef tms", "earth tms", "e/f tms"])
        timestamp = find_field(["timestamp", "date"])
        engineer = find_field(["engineer", "employee", "submitted by", "name of"])

        # Fallback: If Division wasn't in a column explicitly named "Division",
        # check if a conditional section header itself carries the Division name (e.g., "Substation [Amarwara]")
        if not division:
            for col in df.columns:
                val = clean_cell_str(row[col])
                if val and "[" in col and "]" in col:
                    bracket_part = col.split("[")[-1].split("]")[0].strip()
                    if bracket_part and bracket_part.lower() not in ("substation", "feeder", "ptr"):
                        division = bracket_part.upper()
                        break

        # Skip rows where Division is completely blank (no conditional section filled)
        if not division:
            continue

        # Collect all non-blank fields from this row so no custom form field is lost
        non_blank_details = {
            col.split("__")[0]: clean_cell_str(row[col])
            for col in df.columns
            if clean_cell_str(row[col]) != ""
        }

        parsed_rows.append({
            "Timestamp": timestamp,
            "Circle": circle.upper() if circle else "CHHINDWARA",
            "Division": division.upper(),
            "33kV Source Feeder": feeder_33kv,
            "33/11kV Substation": substation.upper() if substation else "UNSPECIFIED S/S",
            "SS Code": ss_code,
            "PTR Capacity": ptr_raw if ptr_raw else "5 MVA",
            "11kV Feeder Code": feeder_code,
            "11kV Feeder Name": feeder_11kv,
            "CT Ratio": ct_ratio if ct_ratio else "200/5 A",
            "CT Primary (A)": extract_first_number(ct_ratio, 200.0),
            "51 Pickup (A)": extract_first_number(oc_pickup, 150.0),
            "51 TMS": extract_first_number(oc_tms, 0.08),
            "ANSI 50 High-Set": hs_50 if hs_50 else "Disabled",
            "50 Pickup (A)": extract_first_number(hs_50, 0.0),
            "51N/51G Pickup (A)": extract_first_number(ef_pickup, 20.0),
            "51N/51G TMS": extract_first_number(ef_tms, 0.05),
            "Engineer": engineer,
            "_raw_non_blank": non_blank_details
        })

    return pd.DataFrame(parsed_rows)


@st.cache_data(ttl=600, max_entries=3, show_spinner=False)
def load_form_responses_from_sheet(sheet_url):
    """
    Fetches the 'Form Responses' tab from the Google Sheet URL (trying XLSX workbook tab scan
    first, then gviz CSV for 'Form Responses 1' / 'Form Responses').
    """
    sheet_id = extract_sheet_id(sheet_url)

    # 1. Try full XLSX export to locate any tab containing 'form response'
    xlsx_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    try:
        resp = requests.get(xlsx_url, timeout=15)
        if resp.status_code == 200 and resp.content[:2] == b"PK":
            xls = pd.ExcelFile(io.BytesIO(resp.content))
            for tab in xls.sheet_names:
                if "form" in tab.lower() and "response" in tab.lower():
                    df_raw = pd.read_excel(xls, sheet_name=tab)
                    df_parsed = parse_conditional_form_responses(df_raw)
                    return df_parsed, tab, "LIVE_GSHEET"
    except Exception:
        pass

    # 2. Try direct gviz CSV endpoint for standard Google Form tab names
    for candidate_tab in ["Form Responses 1", "Form Responses", "Form Responses 2", "Responses"]:
        encoded = urllib.parse.quote(candidate_tab)
        csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={encoded}"
        try:
            r_csv = requests.get(csv_url, timeout=8)
            if r_csv.status_code == 200 and "html" not in r_csv.text[:100].lower():
                df_raw = pd.read_csv(io.StringIO(r_csv.text))
                df_parsed = parse_conditional_form_responses(df_raw)
                if not df_parsed.empty:
                    return df_parsed, candidate_tab, "LIVE_GSHEET"
        except Exception:
            continue

    return pd.DataFrame(), "Not Found", "NO_FORM_TAB"


def push_relay_schedule_to_division_tab(gas_url, payload):
    if not gas_url or not str(gas_url).startswith("https://script.google.com"):
        return {"status": "error", "message": "Invalid Google Apps Script Web App URL."}
    try:
        resp = requests.post(
            gas_url.strip(),
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=20
        )
        if resp.status_code == 200:
            return resp.json()
        return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text[:150]}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------
# 2. PROTECTION MATH ENGINE
# ---------------------------------------------------------
def calc_idmt_time(fault_current, pickup_current, tms, curve_constant):
    if not pickup_current or pickup_current <= 0 or not tms or tms <= 0:
        return None
    psm = fault_current / pickup_current
    if psm <= 1.0:
        return None
    return (curve_constant / ((psm ** 0.02) - 1.0)) * tms


def calc_composite_time(fault_current, idmt_pickup, tms, curve_constant, hs_enabled=False, hs_pickup=None, hs_delay_sec=0.05):
    t_idmt = calc_idmt_time(fault_current, idmt_pickup, tms, curve_constant)
    if hs_enabled and hs_pickup and hs_pickup > 0 and fault_current >= hs_pickup:
        return min(t_idmt, hs_delay_sec) if t_idmt is not None else hs_delay_sec
    return t_idmt


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


# ---------------------------------------------------------
# 3. STREAMLIT WEB APP UI
# ---------------------------------------------------------
st.set_page_config(page_title="MPPKVVCL Form Responses & Relay Suite", layout="wide")

with st.sidebar:
    st.title("⚡ MPPKVVCL Relay Suite")
    st.header("1. Google Sheet (Form Responses)")
    active_sheet_url = st.text_input(
        "Google Sheet URL (with Form Responses tab)",
        value=TARGET_OUTPUT_SHEET_URL,
        help="Paste the Google Sheet URL containing the 'Form Responses 1' tab."
    )
    uploaded_form_file = st.file_uploader("Or Upload Form Responses (.xlsx / .csv)", type=["xlsx", "csv"])

    if st.button("🔄 Refresh & Reload Form Responses"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.header("2. Target Sync Settings")
    gas_webhook_url = st.text_input("Apps Script Web App URL", value=DEFAULT_GAS_WEBHOOK_URL)
    engineer_name = st.text_input("Engineer Name", value="Pallav Swarnkar")
    ngr_ohms = st.number_input("PTR Neutral Earth Resistance (NGR Ω)", min_value=0.0, max_value=500.0, value=40.0, step=1.0)
    cb_open_ms = st.number_input("VCB (52) Interrupting Time (ms)", min_value=20.0, max_value=200.0, value=60.0, step=5.0)
    overshoot_ms = st.number_input("Relay Overshoot Margin (ms)", min_value=10.0, max_value=200.0, value=40.0, step=5.0)
    total_cti_sec = (cb_open_ms + overshoot_ms) / 1000.0

# Load Form Responses Data
if uploaded_form_file is not None:
    if uploaded_form_file.name.endswith(".csv"):
        df_raw_form = pd.read_csv(uploaded_form_file)
    else:
        xls_f = pd.ExcelFile(uploaded_form_file)
        form_tabs = [t for t in xls_f.sheet_names if "form" in t.lower() or "response" in t.lower()]
        chosen_t = form_tabs[0] if form_tabs else xls_f.sheet_names[0]
        df_raw_form = pd.read_excel(xls_f, sheet_name=chosen_t)
    df_form = parse_conditional_form_responses(df_raw_form)
    form_tab_name = uploaded_form_file.name
    form_status = "LOCAL_FILE"
else:
    df_form, form_tab_name, form_status = load_form_responses_from_sheet(active_sheet_url)

st.title("📋 Form Responses Relay Setting Viewer & Coordination Analyzer")

if df_form.empty:
    st.warning(
        f"⚠️ No populated rows found in `{form_tab_name}` from the Google Sheet URL. "
        "Make sure the Google Sheet has a `Form Responses 1` tab with filled Division entries and link-sharing enabled (`Anyone with the link can view`), or upload the `.xlsx`/`.csv` file in the sidebar."
    )
    st.stop()

st.success(f"✅ Loaded **{len(df_form)}** valid submissions from tab **`{form_tab_name}`** (blank conditional sections automatically skipped).")

# ---------------------------------------------------------
# 4. CONDITIONAL DIVISION & SUBSTATION SELECTOR (SKIPS BLANKS)
# ---------------------------------------------------------
# Only list Divisions that actually have non-blank filled entries
filled_divisions = sorted([d for d in df_form["Division"].dropna().unique().tolist() if str(d).strip() != ""])

sel_col1, sel_col2, sel_col3 = st.columns(3)
with sel_col1:
    selected_division = st.selectbox(
        "1️⃣ Select Division (Only Divisions with Filled Data)",
        filled_divisions,
        index=0
    )

df_div_form = df_form[df_form["Division"] == selected_division].reset_index(drop=True)

with sel_col2:
    filled_substations = sorted([s for s in df_div_form["33/11kV Substation"].dropna().unique().tolist() if str(s).strip() != ""])
    selected_ss = st.selectbox(
        "2️⃣ Select 33/11kV Substation",
        ["ALL SUBSTATIONS IN DIVISION"] + filled_substations,
        index=0
    )

if selected_ss != "ALL SUBSTATIONS IN DIVISION":
    df_filtered = df_div_form[df_div_form["33/11kV Substation"] == selected_ss].reset_index(drop=True)
else:
    df_filtered = df_div_form

with sel_col3:
    st.metric(
        f"Filled Entries in {selected_division}",
        f"{len(df_filtered)} Records",
        f"{len(filled_substations)} Substations"
    )

# Display Clean Coalesced Table (Dropping any columns that are completely blank for this Division)
display_cols = [
    "Timestamp", "Circle", "Division", "33kV Source Feeder", "33/11kV Substation",
    "SS Code", "PTR Capacity", "11kV Feeder Code", "11kV Feeder Name",
    "CT Ratio", "51 Pickup (A)", "51 TMS", "ANSI 50 High-Set",
    "51N/51G Pickup (A)", "51N/51G TMS", "Engineer"
]
clean_view_df = df_filtered[display_cols].loc[:, (df_filtered[display_cols] != "").any(axis=0)]

st.subheader(f"📊 Filled Relay Settings from `{form_tab_name}` — `{selected_division}`")
st.dataframe(clean_view_df, use_container_width=True)

# ---------------------------------------------------------
# 5. INSPECT INDIVIDUAL SUBMISSION (NON-BLANK FIELDS ONLY) & PLOT TCC
# ---------------------------------------------------------
st.divider()
st.subheader("🔍 Inspect Single Form Submission & Verify TCC Curve")

record_labels = [
    f"Row {i+1}: {r['33/11kV Substation']} | {r['11kV Feeder Code']} {r['11kV Feeder Name']} ({r['Timestamp']})"
    for i, r in df_filtered.iterrows()
]
selected_rec_label = st.selectbox("Select Submission Record to Inspect / Coordinate:", record_labels, index=0)
rec_idx = record_labels.index(selected_rec_label)
rec = df_filtered.iloc[rec_idx]

ic1, ic2 = st.columns([1, 2])

with ic1:
    st.markdown("**Non-Blank Conditional Section Answers:**")
    non_blank_items = [{"Form Question": k, "Filled Answer": v} for k, v in rec["_raw_non_blank"].items()]
    st.dataframe(pd.DataFrame(non_blank_items), use_container_width=True, height=340)

with ic2:
    st.markdown("**Live IDMT + ANSI 50 Curve Preview from Form Data**")
    curve_type = st.selectbox("IDMT Curve Constant", list(CURVE_CONSTANTS.keys()), index=0)
    c_val = CURVE_CONSTANTS[curve_type]

    p_51 = float(rec["51 Pickup (A)"]) if rec["51 Pickup (A)"] > 0 else 150.0
    tms_51 = float(rec["51 TMS"]) if rec["51 TMS"] > 0 else 0.08
    p_50 = float(rec["50 Pickup (A)"])
    en_50 = p_50 > 0

    p_51n = float(rec["51N/51G Pickup (A)"]) if rec["51N/51G Pickup (A)"] > 0 else 20.0
    tms_51n = float(rec["51N/51G TMS"]) if rec["51N/51G TMS"] > 0 else 0.05

    fc_range = list(range(10, 6000, 20))
    x_oc, y_oc = build_curve_trace(fc_range, p_51, tms_51, c_val, en_50, p_50, 0.05)
    x_ef, y_ef = build_curve_trace(fc_range, p_51n, tms_51n, c_val, False, None, 0.05)

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x_oc, y=y_oc, name=f"Phase 51/50 ({p_51:.0f}A, TMS {tms_51})", line=dict(color="#d32f2f", width=3)))
    fig.add_trace(go.Scatter(x=x_ef, y=y_ef, name=f"Earth 51N ({p_51n:.0f}A, TMS {tms_51n})", line=dict(color="#0288d1", width=3, dash="dot")))
    fig.update_xaxes(type="log", title_text="Fault Current (A)")
    fig.update_yaxes(type="log", title_text="Operating Time (s)", range=[-1.6, 2.0])
    fig.update_layout(height=340, margin=dict(t=20, b=20))
    st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------
# 6. PUSH CLEANED DIVISION DATA TO DIVISION TAB IN TARGET SHEET
# ---------------------------------------------------------
if st.button(f"☁️ Sync Cleaned `{selected_division}` Form Records to `{selected_division}` Tab", type="primary", use_container_width=True):
    rows_to_push = []
    for _, r in df_filtered.iterrows():
        ct_p = float(r["CT Primary (A)"]) if r["CT Primary (A)"] > 0 else 200.0
        rows_to_push.append({
            "Bay / Feeder Code": r["11kV Feeder Code"] or r["SS Code"],
            "Bay / Protection Zone Name": r["11kV Feeder Name"] or r["33/11kV Substation"],
            "Voltage": "11 kV",
            "CT Ratio": r["CT Ratio"],
            "Peak Load (A)": "",
            "51 Pickup (A) / Plug (%)": f"{r['51 Pickup (A)']:.0f} A ({(r['51 Pickup (A)']/ct_p)*100:.1f}%)",
            "51 TMS": r["51 TMS"],
            "ANSI 50 High-Set": r["ANSI 50 High-Set"],
            "51N/51G Pickup (A) / Plug (%)": f"{r['51N/51G Pickup (A)']:.0f} A ({(r['51N/51G Pickup (A)']/ct_p)*100:.1f}%)",
            "51N/51G TMS": r["51N/51G TMS"]
        })

    res = push_relay_schedule_to_division_tab(gas_webhook_url, {
        "engineer": engineer_name,
        "circle": df_filtered.iloc[0]["Circle"],
        "division": selected_division,
        "feeder_33kv": df_filtered.iloc[0]["33kV Source Feeder"],
        "feeder_33kv_code": "",
        "substation": df_filtered.iloc[0]["33/11kV Substation"],
        "ss_code": df_filtered.iloc[0]["SS Code"],
        "ptr_label": df_filtered.iloc[0]["PTR Capacity"],
        "ptr_z_pct": "",
        "ngr_ohms": ngr_ohms,
        "cti_ms": total_cti_sec * 1000.0,
        "rows": rows_to_push
    })
    if res.get("status") == "success":
        st.success(f"✅ Synced {res.get('rowsSaved')} cleaned records into **[{res.get('tabName')}]**!")
    else:
        st.error(f"❌ Sync Error: {res.get('message')}")
