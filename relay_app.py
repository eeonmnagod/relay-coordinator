import io
import json
import re
from datetime import datetime, date
import pytz
import requests
import streamlit as st
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from streamlit_js_eval import get_geolocation
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ==========================================
# CONFIGURATION
# ==========================================
st.set_page_config(
    page_title="Field Data & Geotagged Photo Portal",
    page_icon="📍",
    layout="centered",
)

IST = pytz.timezone("Asia/Kolkata")

DEFAULT_FORM_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSdQweoxwsCjzBOg6D5SBVirVnTyWXoLzN8u0FWHDgY5z5MGqg/viewform"
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ==========================================
# GOOGLE CLOUD AUTHENTICATION
# ==========================================
@st.cache_resource
def get_gcp_clients():
    """Authenticate with Google Sheets and Google Drive via Service Account."""
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    return gc, drive_service


# ==========================================
# GOOGLE FORM PARSER (FB_PUBLIC_LOAD_DATA_)
# ==========================================
@st.cache_data(ttl=600)
def load_google_form_schema(form_url: str) -> dict:
    """
    Fetch the Google Form viewform HTML, extract FB_PUBLIC_LOAD_DATA_,
    and parse all sections, questions, options, and conditional branching rules.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(form_url, headers=headers, timeout=15)
    resp.raise_for_status()
    html = resp.text

    match = re.search(
        r"var\s+FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\]);\s*</script>",
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError(
            "Could not extract FB_PUBLIC_LOAD_DATA_ from the Google Form. "
            "In Google Forms Settings, ensure 'Limit to 1 response' and native "
            "'File Upload' questions are turned OFF so the form can be read publicly."
        )

    raw_data = json.loads(match.group(1))

    form_title = "Field Data Collection Form"
    if len(raw_data) > 1 and isinstance(raw_data[1], list):
        if len(raw_data[1]) > 8 and raw_data[1][8]:
            form_title = raw_data[1][8]
    if form_title == "Field Data Collection Form" and len(raw_data) > 3 and raw_data[3]:
        form_title = raw_data[3]

    form_desc = (
        raw_data[1][0]
        if (len(raw_data) > 1 and isinstance(raw_data[1], list) and raw_data[1][0])
        else ""
    )

    raw_items = (
        raw_data[1][1]
        if (len(raw_data) > 1 and isinstance(raw_data[1], list) and len(raw_data[1]) > 1)
        else []
    )

    sections = [
        {
            "id": 0,
            "title": form_title,
            "description": form_desc,
            "items": [],
            "next_section": -1,
        }
    ]

    for item in raw_items:
        if not isinstance(item, list) or len(item) < 4:
            continue

        item_id = item[0]
        title = (item[1] or "").strip()
        desc = (item[2] or "").strip()
        q_type = item[3]

        if q_type == 8:
            if len(item) > 5 and isinstance(item[5], int):
                sections[-1]["next_section"] = item[5]

            sections.append(
                {
                    "id": item_id,
                    "title": title,
                    "description": desc,
                    "items": [],
                    "next_section": -1,
                }
            )
            continue

        if q_type == 6:
            sections[-1]["items"].append(
                {
                    "id": item_id,
                    "title": title,
                    "description": desc,
                    "type": q_type,
                    "required": False,
                }
            )
            continue

        if len(item) > 4 and isinstance(item[4], list) and len(item[4]) > 0:
            q_meta = item[4][0]
            entry_id = q_meta[0]
            raw_options = q_meta[1] if (len(q_meta) > 1 and isinstance(q_meta[1], list)) else []
            required = bool(q_meta[2]) if len(q_meta) > 2 else False

            options = []
            has_branching = False
            for opt in raw_options:
                if not isinstance(opt, list):
                    continue
                opt_label = str(opt[0]) if opt[0] is not None else ""
                go_to = opt[2] if (len(opt) > 2 and isinstance(opt[2], int)) else None
                is_other = bool(opt[4]) if (len(opt) > 4 and opt[4]) else False
                if go_to is not None:
                    has_branching = True
                options.append(
                    {
                        "label": opt_label,
                        "go_to": go_to,
                        "is_other": is_other,
                    }
                )

            scale_labels = (
                q_meta[3] if (len(q_meta) > 3 and isinstance(q_meta[3], list)) else ["", ""]
            )

            sections[-1]["items"].append(
                {
                    "id": item_id,
                    "entry_id": entry_id,
                    "title": title,
                    "description": desc,
                    "type": q_type,
                    "required": required,
                    "options": options,
                    "has_branching": has_branching,
                    "scale_labels": scale_labels,
                }
            )

    return {
        "title": form_title,
        "description": form_desc,
        "sections": sections,
    }


# ==========================================
# PHOTO WATERMARK & GOOGLE DRIVE UPLOAD
# ==========================================
def watermark_image(image_bytes: bytes, metadata_lines: list[str]) -> io.BytesIO:
    """Stamp timestamp, GPS coordinates, and key form answers onto the photo."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    max_width = 1280
    if img.width > max_width:
        ratio = max_width / float(img.width)
        img = img.resize((max_width, int(img.height * ratio)), Image.Resampling.LANCZOS)

    draw = ImageDraw.Draw(img, "RGBA")
    font = ImageFont.load_default()

    padding = 12
    line_height = 18
    banner_height = (len(metadata_lines) * line_height) + (padding * 2)

    draw.rectangle(
        [(0, img.height - banner_height), (img.width, img.height)],
        fill=(0, 0, 0, 175),
    )

    y = img.height - banner_height + padding
    for line in metadata_lines:
        draw.text((padding, y), line, fill=(255, 255, 255, 255), font=font)
        y += line_height

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf


def upload_to_drive(drive_service, file_buffer: io.BytesIO, filename: str, folder_id: str) -> str:
    """Upload watermarked JPEG to Google Drive and return its shareable link."""
    metadata = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(file_buffer, mimetype="image/jpeg", resumable=True)

    created = (
        drive_service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    file_id = created.get("id")

    try:
        drive_service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()
    except Exception:
        pass

    return created.get(
        "webViewLink", f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
    )


# ==========================================
# DYNAMIC GOOGLE SHEETS LOGGING
# ==========================================
def save_response_to_sheet(gc, sheet_id: str, worksheet_name: str, record: dict):
    """
    Dynamically match or create column headers in Row 1 of the Google Sheet
    and append the submitted record.
    """
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows="1000", cols="30")

    existing_headers = ws.row_values(1)

    if not existing_headers:
        existing_headers = list(record.keys())
        ws.append_row(existing_headers, value_input_option="USER_ENTERED")
    else:
        header_map_lower = {h.strip().lower(): h for h in existing_headers}
        new_cols = []
        for key in record.keys():
            if key.strip().lower() not in header_map_lower:
                new_cols.append(key)
                header_map_lower[key.strip().lower()] = key

        if new_cols:
            existing_headers.extend(new_cols)
            ws.update(
                range_name="A1",
                values=[existing_headers],
                value_input_option="USER_ENTERED",
            )

    record_lower = {k.strip().lower(): v for k, v in record.items()}
    row_values = [record_lower.get(col.strip().lower(), "") for col in existing_headers]
    ws.append_row(row_values, value_input_option="USER_ENTERED")


# ==========================================
# QUESTION RENDERER WITH CONDITIONAL LOGIC
# ==========================================
def render_question(q: dict) -> tuple[any, int | None]:
    """
    Render a single Google Form question in Streamlit.
    Returns (answer_value, branch_target_section_id).
    """
    q_id = q["id"]
    q_type = q["type"]
    title = q["title"]
    desc = q.get("description", "")
    req_mark = " *" if q.get("required") else ""
    label = f"**{title}{req_mark}**"

    if q_type == 6:
        st.markdown(f"#### {title}")
        if desc:
            st.caption(desc)
        return None, None

    options = q.get("options", [])
    has_branching = q.get("has_branching", False)
    branch_target = None

    if q_type == 0:
        val = st.text_input(label, help=desc if desc else None, key=f"q_{q_id}")
        return val.strip(), None

    if q_type == 1:
        val = st.text_area(label, help=desc if desc else None, key=f"q_{q_id}")
        return val.strip(), None

    if q_type in (2, 3):
        opt_labels = [
            "Other (Specify below)" if o["is_other"] else o["label"] for o in options
        ]
        display_options = ["-- Select --"] + opt_labels

        if q_type == 2 and len(opt_labels) <= 6 and not has_branching:
            choice = st.radio(
                label,
                options=display_options,
                help=desc if desc else None,
                key=f"q_{q_id}",
            )
        else:
            choice = st.selectbox(
                label,
                options=display_options,
                help=desc if desc else None,
                key=f"q_{q_id}",
            )

        if choice == "-- Select --":
            return "", None

        selected_idx = opt_labels.index(choice)
        selected_opt = options[selected_idx]
        branch_target = selected_opt.get("go_to")

        if selected_opt.get("is_other"):
            other_val = st.text_input(
                f"Specify Other for '{title}' *", key=f"q_{q_id}_other"
            )
            return other_val.strip(), branch_target

        return choice, branch_target

    if q_type == 4:
        opt_labels = [
            "Other" if o["is_other"] else o["label"] for o in options
        ]
        selected_list = st.multiselect(
            label,
            options=opt_labels,
            help=desc if desc else None,
            key=f"q_{q_id}",
        )
        if "Other" in selected_list:
            other_val = st.text_input(
                f"Specify Other for '{title}' *", key=f"q_{q_id}_other"
            ).strip()
            selected_list = [
                other_val if item == "Other" else item for item in selected_list if item
            ]
        return ", ".join(selected_list), None

    if q_type == 5:
        nums = [int(o["label"]) for o in options if o["label"].isdigit()]
        min_v, max_v = (min(nums), max(nums)) if nums else (1, 5)
        val = st.slider(
            label,
            min_value=min_v,
            max_value=max_v,
            help=desc if desc else None,
            key=f"q_{q_id}",
        )
        return str(val), None

    if q_type == 9:
        val = st.date_input(
            label, value=date.today(), help=desc if desc else None, key=f"q_{q_id}"
        )
        return val.strftime("%m/%d/%Y") if val else "", None

    if q_type == 10:
        val = st.time_input(
            label, value=datetime.now(IST).time(), help=desc if desc else None, key=f"q_{q_id}"
        )
        return val.strftime("%H:%M") if val else "", None

    val = st.text_input(label, help=desc if desc else None, key=f"q_{q_id}")
    return val.strip(), None


# ==========================================
# MAIN APPLICATION UI
# ==========================================
form_url = st.secrets.get("app_config", {}).get("form_url", DEFAULT_FORM_URL)

try:
    schema = load_google_form_schema(form_url)
except Exception as e:
    st.error(f"Error loading Google Form schema: {e}")
    st.stop()

st.title(f"📋 {schema['title']}")
if schema["description"]:
    st.markdown(schema["description"])

st.divider()

# --- 1. LIVE GPS CAPTURE ---
st.subheader("📍 1. Live Location Capture (GPS)")
loc_data = get_geolocation(component_key="browser_gps")

lat, lon, acc = None, None, None
if loc_data and "coords" in loc_data:
    lat = loc_data["coords"].get("latitude")
    lon = loc_data["coords"].get("longitude")
    acc = loc_data["coords"].get("accuracy")
    st.success(f"GPS Locked: `{lat:.6f}, {lon:.6f}` (Accuracy: ±{acc:.1f} m)")
    st.map(pd.DataFrame({"lat": [lat], "lon": [lon]}), zoom=15)
else:
    st.warning("Waiting for browser GPS permission... Please click 'Allow' when prompted.")

with st.expander("Manual Coordinates Fallback (Optional)"):
    c1, c2 = st.columns(2)
    m_lat = c1.number_input("Latitude", value=lat if lat else 22.057400, format="%.6f")
    m_lon = c2.number_input("Longitude", value=lon if lon else 78.938200, format="%.6f")
    if (lat is None or lon is None) and st.checkbox("Use manual coordinates"):
        lat, lon, acc = m_lat, m_lon, 0.0

st.divider()

# --- 2. DYNAMIC GOOGLE FORM SECTIONS & CONDITIONAL BRANCHING ---
st.subheader("📝 2. Form Details")

sections = schema["sections"]
sec_id_to_idx = {sec["id"]: idx for idx, sec in enumerate(sections)}

answers = {}
missing_required = []
visited_sections = set()
current_idx = 0
awaiting_branch_choice = False

while (
    current_idx is not None
    and 0 <= current_idx < len(sections)
    and current_idx not in visited_sections
):
    visited_sections.add(current_idx)
    sec = sections[current_idx]

    if current_idx > 0 and sec["title"]:
        st.markdown(f"### {sec['title']}")
        if sec["description"]:
            st.caption(sec["description"])

    section_branch_target = None
    section_has_unanswered_branch = False

    for q in sec["items"]:
        ans, branch_to = render_question(q)
        if q["type"] != 6:
            if ans != "" or q["title"] not in answers:
                answers[q["title"]] = ans

            if q.get("required") and not ans:
                missing_required.append(q["title"])

            if q.get("has_branching"):
                if not ans:
                    section_has_unanswered_branch = True
                elif branch_to is not None:
                    section_branch_target = branch_to

    if section_has_unanswered_branch:
        awaiting_branch_choice = True
        break

    target = (
        section_branch_target
        if section_branch_target is not None
        else sec.get("next_section", -1)
    )

    if target == -2:
        break
    elif target in sec_id_to_idx:
        current_idx = sec_id_to_idx[target]
    else:
        current_idx += 1

st.divider()

# --- 3. CAMERA / PHOTO CAPTURE ---
st.subheader("📷 3. Site / Equipment Photo")
photo_source = st.radio(
    "Select Photo Input Method",
    options=["Take Live Camera Photo", "Upload Photo(s) from Device"],
    horizontal=True,
)

uploaded_photos = []
if photo_source == "Take Live Camera Photo":
    cam_pic = st.camera_input("Capture Photo *")
    if cam_pic:
        uploaded_photos = [cam_pic]
else:
    files = st.file_uploader(
        "Upload Photo(s) (JPG/PNG) *",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
    )
    if files:
        uploaded_photos = files

st.divider()

# --- 4. SUBMIT TO GOOGLE DRIVE & GOOGLE SHEETS ---
if awaiting_branch_choice:
    st.info("👆 Please select an option above to load the next section of the form.")
else:
    if st.button("✅ Submit Response", type="primary", use_container_width=True):
        errors = []
        if missing_required:
            errors.append(
                f"Please fill in all required fields: {', '.join(missing_required)}"
            )
        if not uploaded_photos:
            errors.append("Please capture or upload at least one photo.")
        if lat is None or lon is None:
            errors.append(
                "GPS Location is missing. Please allow browser location access or enable manual coordinates."
            )

        if errors:
            for err in errors:
                st.error(err)
        else:
            with st.spinner("Watermarking photo(s), uploading to Drive, and saving to Sheet..."):
                try:
                    now_ist = datetime.now(IST)
                    timestamp_str = now_ist.strftime("%m/%d/%Y %H:%M:%S")
                    maps_url = f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}"

                    gc, drive_service = get_gcp_clients()
                    folder_id = st.secrets["app_config"]["drive_folder_id"]
                    sheet_id = st.secrets["app_config"]["spreadsheet_id"]
                    worksheet_name = st.secrets["app_config"].get(
                        "worksheet_name", "Form Responses 1"
                    )

                    summary_pairs = [
                        f"{k}: {v}" for k, v in list(answers.items())[:2] if v
                    ]
                    watermark_lines = [
                        f"Time: {timestamp_str} IST | GPS: {lat:.6f}, {lon:.6f}",
                        " | ".join(summary_pairs)[:90] if summary_pairs else schema["title"],
                    ]

                    drive_links = []
                    preview_buffers = []
                    for idx, photo_file in enumerate(uploaded_photos, start=1):
                        wm_buf = watermark_image(photo_file.getvalue(), watermark_lines)
                        preview_buffers.append(wm_buf)

                        fname = f"FieldPhoto_{now_ist.strftime('%Y%m%d_%H%M%S')}_{idx}.jpg"
                        link = upload_to_drive(drive_service, wm_buf, fname, folder_id)
                        drive_links.append(link)

                    record = {"Timestamp": timestamp_str}
                    record.update(answers)
                    record["Latitude"] = round(lat, 6)
                    record["Longitude"] = round(lon, 6)
                    record["Accuracy (m)"] = round(acc, 1) if acc else ""
                    record["Google Maps Link"] = maps_url
                    record["Photo Drive URL"] = " , ".join(drive_links)

                    save_response_to_sheet(gc, sheet_id, worksheet_name, record)

                    st.success("🎉 Response & Geotagged Photo(s) saved successfully!")
                    st.markdown(f"- **Location Pin:** [Open in Google Maps]({maps_url})")
                    for i, link in enumerate(drive_links, start=1):
                        st.markdown(f"- **Uploaded Photo {i}:** [View in Google Drive]({link})")

                    for buf in preview_buffers:
                        buf.seek(0)
                        st.image(buf, caption="Watermarked & Uploaded Photo", use_container_width=True)

                except Exception as e:
                    st.error(f"Submission failed: {e}")
