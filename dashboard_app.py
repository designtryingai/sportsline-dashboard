"""Public read-only dashboard for the Best Bets tab of the Sportsbook Model sheet.

This app never writes to the Google Sheet. It only reads the "Best Bets"
worksheet, scores each row, and displays the top 4 per bet type (Spread,
Total, Moneyline) in a card grid.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEET_ID = "1nEtrFPgESIzuBXf3bGSxVbZ1kUJyFx6-xGKjPJVqthM"
TAB_NAME = "Best Bets"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]

ERROR_EDGE_THRESHOLD = 20.0
SECTIONS = ["Spread", "Total", "Moneyline"]
SEASON_START = pd.Timestamp(2026, 9, 9)

# Columns dropped entirely from card display: always-blank grading/tracking
# columns (unused until Phase 6.6) plus Confidence, which is always just
# Grade spelled out as a word and carries no extra information.
DROPPED_COLUMNS = [
    "Spread Movement Direction",
    "Units Risked",
    "Result",
    "Units Won/Lost",
    "Confidence",
]

# Flags as they literally appear in the "Profitable Segment Match" column.
# The column joins multiple flags with ", " and several flag names
# themselves contain a comma (e.g. "Total, Big Edge"), so a naive split on
# "," shreds them. Matching each known flag as a substring instead sidesteps
# that ambiguity entirely.
KNOWN_FLAGS = [
    "Home Favorite Moneyline",
    "Warm Outdoor Total",
    "Total, Graded B or A",
    "Total, Big Edge",
    "Total, Medium Edge",
    "Total, Graded A",
]

FLAG_WEIGHTS = {
    "Home Favorite Moneyline": 22.9,
    "Warm Outdoor Total": 15.0,
    "Total, Graded B": 11.5,
    "Total, Big Edge": 9.1,
    "Total, Medium Edge": 5.2,
    "Total, Graded A": 2.1,
}

# Badge label -> (slug, qualifies description, track record line)
GLOSSARY = {
    "Home Favorite ML": (
        "glossary-home-fav-ml",
        "Qualifies when: the bet is a Moneyline, the model's pick is the home team, and the odds are between -100 and -300.",
        "Since 2024: 98 bets, 77.6% win rate, +22.9% ROI",
    ),
    "Warm Outdoor Total": (
        "glossary-warm-outdoor-total",
        "Qualifies when: the bet is a Total played outdoors in warm conditions.",
        "All 3 seasons (2023-2025): 207 bets, 60.5% win rate, +15.0% ROI",
    ),
    "Graded B": (
        "glossary-graded-b",
        "Qualifies when: the bet is a Total, and SportsLine's own model gave the pick a letter grade of B.",
        "Since 2024: 111 bets, 58.6% win rate, +11.5% ROI",
    ),
    "Big Edge": (
        "glossary-big-edge",
        "Qualifies when: the bet is a Total, and the model's Sim Probability is 4.0 percentage points or more above what the market's odds imply.",
        "Since 2024: 217 bets, 57.1% win rate, +9.1% ROI",
    ),
    "Medium Edge": (
        "glossary-medium-edge",
        "Qualifies when: the bet is a Total, and the model's Sim Probability is moderately above what the market's odds imply (below the Big Edge threshold).",
        "Since 2024: 88 bets, 54.5% win rate, +5.2% ROI",
    ),
    "Graded A": (
        "glossary-graded-a",
        "Qualifies when: the bet is a Total, and SportsLine's own model gave the pick a letter grade of A.",
        "Since 2024: 15 bets, 53.3% win rate, +2.1% ROI",
    ),
    "Potential Sportsline Error": (
        "glossary-sl-error",
        "Not a proven segment, this is a data-quality caution. Fires when a row's Edge % is 20 points or more in either direction, well outside the range any validated segment actually covers. Usually means a scraper misread a line or matched the wrong team. The pick still counts toward its ranking, just double-check it before betting.",
        "Threshold: |Edge %| ≥ 20",
    ),
}

st.set_page_config(page_title="Best Bets", layout="wide")

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800&family=Archivo:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

    :root {
        --bg: #10140F;
        --text: #E7E3D8;
        --muted: #8A8577;
        --rule: #33392E;
        --rule-soft: #23281F;
        --pos: #3FCB77;
        --neg: #5E86BD;
        --caution: #B37A50;
        --sans: 'Archivo', system-ui, sans-serif;
        --display: 'Big Shoulders Display', 'Archivo', sans-serif;
        --mono: 'IBM Plex Mono', ui-monospace, monospace;
        --cols: 230px 86px 120px 56px 46px 52px 62px minmax(0, 1fr);
    }

    html, body, [class*="css"], [class*="st-"], .stApp, .stApp * {
        font-family: var(--sans) !important;
    }
    .stApp { background-color: var(--bg); }

    /* Streamlit's own chrome, hidden so the page reads as one design. */
    #MainMenu, header, footer { visibility: hidden; }
    .block-container { padding-top: 2rem; }

    .bb-wrap { max-width: 1080px; margin: 0 auto; }

    /* ---- page header ---- */
    .bb-head {
        display: flex; align-items: flex-end; justify-content: space-between;
        border-bottom: 2px solid var(--rule); padding-bottom: 14px; margin-bottom: 30px;
    }
    .bb-head h1 {
        font-family: var(--display) !important; font-weight: 800; font-size: 46px;
        line-height: 1; margin: 0; color: var(--text);
    }
    .bb-head .bb-sub { font-size: 13px; color: var(--muted); margin-top: 2px; }
    .bb-stamp { text-align: right; font-family: var(--mono) !important; font-size: 11px; color: var(--muted); }
    .bb-stamp .next { color: var(--caution); }

    /* ---- sections ---- */
    .bb-section { margin-top: 30px; }
    .bb-section h2 {
        font-family: var(--display) !important; font-weight: 700; font-size: 24px;
        color: var(--text); margin: 0 0 6px 0; letter-spacing: 0;
    }
    .bb-empty-state {
        font-size: 13px; line-height: 1.5; color: var(--muted);
        border-left: 2px solid var(--rule); padding-left: 12px; max-width: 620px;
    }

    /* ---- pick rows ---- */
    .bb-colhead {
        display: grid; grid-template-columns: var(--cols); gap: 10px;
        padding: 6px 0 7px 0; border-bottom: 1px solid var(--rule);
        font-size: 11px; color: var(--muted);
    }
    .bb-row {
        display: grid; grid-template-columns: var(--cols); gap: 10px;
        align-items: center; padding: 9px 0; border-bottom: 1px solid var(--rule-soft);
    }
    .bb-row.starred {
        border-left: 2px solid var(--pos); padding-left: 10px; margin-left: -12px;
    }
    .bb-matchup { font-size: 13px; color: var(--text); }
    .bb-subline { font-size: 11px; color: var(--muted); margin-top: 1px; }
    .bb-topline { font-size: 11px; color: var(--pos); margin-top: 1px; }
    .bb-kick { font-family: var(--mono) !important; font-size: 12px; color: var(--muted); }
    .bb-pick { font-family: var(--mono) !important; font-size: 14px; font-weight: 600; color: var(--text); }
    .bb-num { font-family: var(--mono) !important; font-size: 13px; color: var(--text); text-align: right; }
    .bb-num.dim { color: var(--muted); }
    .bb-seg { display: flex; flex-direction: column; gap: 3px; }
    .bb-seg-line { font-size: 12px; white-space: nowrap; }
    .bb-seg-line .roi { font-family: var(--mono) !important; font-size: 12px; color: var(--pos); margin-left: 6px; }
    a.bb-seg-name {
        color: var(--pos); text-decoration: none; border-bottom: 1px solid var(--rule);
    }
    a.bb-seg-name:hover { color: #6FE09B; border-bottom-color: var(--pos); }
    .bb-caution { font-family: var(--mono) !important; font-size: 11px; color: var(--caution); }

    /* ---- completed picks ---- */
    .bb-results { max-width: 1080px; margin: 34px auto 0 auto; border-top: 2px solid var(--rule); padding-top: 16px; }
    .bb-results h2 { font-family: var(--display) !important; font-weight: 700; font-size: 24px; color: var(--text); margin: 0 0 4px 0; }
    .bb-results-summary { font-family: var(--mono) !important; font-size: 13px; color: var(--muted); margin-bottom: 10px; }
    .bb-results-summary .pos { color: var(--pos); }
    .bb-results-summary .neg { color: var(--neg); }
    .bb-result-row {
        display: grid; grid-template-columns: 230px 86px 176px 46px 62px minmax(0, 1fr);
        gap: 10px; align-items: center; padding: 8px 0;
        border-bottom: 1px solid var(--rule-soft); font-size: 13px; color: var(--text);
    }
    .bb-result-row:last-child { border-bottom: none; }
    .bb-result-type { font-size: 12px; color: var(--muted); }
    .bb-result-pick { font-family: var(--mono) !important; font-size: 13px; }
    .bb-result-pick .odds { color: var(--muted); margin-left: 6px; }
    .bb-result-tag { font-family: var(--mono) !important; font-size: 12px; }
    .bb-result-tag.win { color: var(--pos); }
    .bb-result-tag.loss { color: var(--neg); }
    .bb-result-tag.push { color: var(--muted); }

    /* ---- glossary ---- */
    .bb-glossary { max-width: 1080px; margin: 34px auto 0 auto; border-top: 2px solid var(--rule); padding-top: 16px; }
    .bb-glossary h2 { font-family: var(--display) !important; font-weight: 700; font-size: 24px; color: var(--text); margin: 0 0 10px 0; }
    .bb-glossary-item {
        display: grid; grid-template-columns: 172px minmax(0, 1fr) 150px;
        gap: 16px; align-items: baseline; padding: 7px 0; scroll-margin-top: 20px;
    }
    .bb-glossary-item h3 { font-size: 13px; font-weight: 600; margin: 0; color: var(--text); }
    .bb-glossary-item.error-item h3 { color: var(--caution); }
    .bb-qualifies { font-size: 12px; line-height: 1.5; color: var(--muted); }
    .bb-track-record { font-family: var(--mono) !important; font-size: 12px; color: var(--pos); text-align: right; }
    .bb-glossary-item.error-item .bb-track-record { color: var(--caution); }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=600)
def load_best_bets_and_meta() -> tuple[pd.DataFrame, str]:
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    client = gspread.authorize(creds)
    ws = client.open_by_key(SHEET_ID).worksheet(TAB_NAME)
    values = ws.get_all_values()
    df = pd.DataFrame() if not values else pd.DataFrame(values[1:], columns=values[0])

    drive = build("drive", "v3", credentials=creds)
    meta = drive.files().get(fileId=SHEET_ID, fields="modifiedTime").execute()
    modified_time = meta["modifiedTime"]

    return df, modified_time


def next_wednesday_8pm_et(now_et: datetime) -> datetime:
    days_ahead = (2 - now_et.weekday()) % 7  # Wednesday = 2
    candidate = (now_et + timedelta(days=days_ahead)).replace(
        hour=20, minute=0, second=0, microsecond=0
    )
    if candidate <= now_et:
        candidate += timedelta(days=7)
    return candidate


def _matched_raw_flags(row: pd.Series) -> list[str]:
    match_text = str(row.get("Profitable Segment Match", ""))
    return [flag for flag in KNOWN_FLAGS if flag in match_text]


def _flag_to_label(flag: str, grade: str) -> str | None:
    if flag == "Total, Graded B or A":
        return "Graded B" if grade == "B" else "Graded A" if grade == "A" else None
    if flag == "Home Favorite Moneyline":
        return "Home Favorite ML"
    if flag.startswith("Total, "):
        return flag.split("Total, ", 1)[1]
    return flag


def row_matched_flags(row: pd.Series) -> list[str]:
    """Return the ordered, de-duplicated list of badge labels for a row."""
    grade = str(row.get("Grade", "")).strip().upper()
    labels: list[str] = []
    for flag in _matched_raw_flags(row):
        label = _flag_to_label(flag, grade)
        if label and label in GLOSSARY and label not in labels:
            labels.append(label)
    return labels


def score_row(row: pd.Series) -> float:
    grade = str(row.get("Grade", "")).strip().upper()
    total = 0.0
    for flag in _matched_raw_flags(row):
        if flag == "Total, Graded B or A":
            if grade == "B":
                total += FLAG_WEIGHTS["Total, Graded B"]
            elif grade == "A":
                total += FLAG_WEIGHTS["Total, Graded A"]
        elif flag in FLAG_WEIGHTS:
            total += FLAG_WEIGHTS[flag]
    return total


def _kickoff_key(row: pd.Series) -> datetime:
    try:
        return datetime.strptime(f'{row["Date"]} {row["Time"]}', "%m/%d/%Y %I:%M %p")
    except Exception:
        return datetime.max


def _meta_display(row: pd.Series) -> str:
    kickoff = row["_kickoff"]
    if kickoff == datetime.max:
        return f'{row.get("Date", "")} &middot; {row.get("Time", "")}'
    return f'{kickoff.strftime("%a %-m/%-d")} &middot; {row.get("Time", "")}'


def _pick_display(row: pd.Series) -> tuple[str, str, str]:
    bet_type = row.get("Bet Type", "")
    pick = str(row.get("Pick", "")).strip()
    line = str(row.get("Line", "")).strip()
    odds = row.get("Odds", "")
    if bet_type == "Moneyline":
        return "PICK", pick, odds
    if bet_type == "Total":
        return pick.upper(), line if line else "&mdash;", odds
    # Spread (or any future bet type): team name as the side, spread number
    # as the headline figure. No real data exercises this path yet.
    return pick, line if line else "&mdash;", odds


# Each badge label's headline ROI, shown inline on the pick row so the
# track record is visible without opening the glossary.
FLAG_ROI = {
    "Home Favorite ML": "+22.9%",
    "Warm Outdoor Total": "+15.0%",
    "Graded B": "+11.5%",
    "Big Edge": "+9.1%",
    "Medium Edge": "+5.2%",
    "Graded A": "+2.1%",
}

COLUMN_HEADS = [
    ("Matchup", ""),
    ("Kickoff", ""),
    ("Pick", ""),
    ("Odds", "text-align: right;"),
    ("Grade", "text-align: right;"),
    ("Sim", "text-align: right;"),
    ("Edge", "text-align: right;"),
    ("Segment, and its record", ""),
]


def build_card_html(row: pd.Series, is_starred: bool) -> str:
    """One pick, rendered as a single dense row."""
    is_error = row["_edge_num"] is not None and abs(row["_edge_num"]) >= ERROR_EDGE_THRESHOLD

    row_classes = "bb-row starred" if is_starred else "bb-row"

    # Dome and Temperature ride along under the matchup rather than taking
    # their own columns, since they only exist on some rows.
    context_bits = []
    dome = str(row.get("Dome (Y/N)", "")).strip()
    temp = str(row.get("Temperature (F)", "")).strip()
    if dome:
        context_bits.append(f"Dome {dome}")
    if temp:
        context_bits.append(f"{temp}&deg;F")
    subline = f'<div class="bb-subline">{" &nbsp; ".join(context_bits)}</div>' if context_bits else ""
    topline = '<div class="bb-topline">Strongest pick on the board</div>' if is_starred else ""

    seg_lines = ""
    for label in row["_flags"]:
        slug = GLOSSARY[label][0]
        roi = FLAG_ROI.get(label, "")
        roi_html = f'<span class="roi">{roi}</span>' if roi else ""
        seg_lines += (
            f'<div class="bb-seg-line">'
            f'<a href="#{slug}" class="bb-seg-name">{label}</a>{roi_html}</div>'
        )
    if is_error:
        slug = GLOSSARY["Potential Sportsline Error"][0]
        seg_lines += f'<div class="bb-seg-line"><a href="#{slug}" class="bb-caution">Check this row</a></div>'

    grade = str(row.get("Grade", "")).strip()
    sim_prob = row.get("Sim Probability", "")
    edge_display = row.get("Edge %", "")
    pick_side, pick_number, pick_odds = _pick_display(row)

    # Total rows read "Over 39.5"; Moneyline rows are just the team name.
    if str(row.get("Bet Type", "")) == "Moneyline":
        pick_text = pick_number
    else:
        pick_text = f"{pick_side.title()} {pick_number}"

    return "".join([
        f'<div class="{row_classes}">',
        f'<div><div class="bb-matchup">{row.get("Away Team", "")} at {row.get("Home Team", "")}</div>{topline}{subline}</div>',
        f'<div class="bb-kick">{_meta_display(row)}</div>',
        f'<div class="bb-pick">{pick_text}</div>',
        f'<div class="bb-num dim">{pick_odds}</div>',
        f'<div class="bb-num">{grade}</div>',
        f'<div class="bb-num">{sim_prob}%</div>',
        f'<div class="bb-num">{edge_display}</div>',
        f'<div class="bb-seg">{seg_lines}</div>',
        "</div>",
    ])


def build_section_html(section_name: str, section_df: pd.DataFrame, star_key) -> str:
    parts = [
        '<div class="bb-section">',
        f"<h2>{section_name}</h2>",
    ]
    if section_df.empty:
        message = f"Nothing qualifies in {section_name} this week."
        if section_name == "Spread":
            message = (
                "Nothing qualifies. No spread pattern has survived the "
                "season-by-season re-check, so the model has no validated "
                "spread segment to draw from."
            )
        parts.append(f'<div class="bb-empty-state">{message}</div>')
    else:
        heads = "".join(
            f'<div style="{style}">{label}</div>' for label, style in COLUMN_HEADS
        )
        parts.append(f'<div class="bb-colhead">{heads}</div>')
        for _, row in section_df.iterrows():
            row_key = (row["Away Team"], row["Home Team"], row["Bet Type"])
            parts.append(build_card_html(row, is_starred=(row_key == star_key)))
    parts.append("</div>")
    return "".join(parts)


RESULT_VALUES = ["Win", "Loss", "Push"]


def normalize_result(value) -> str:
    """Return "Win"/"Loss"/"Push" for a filled Result cell, or "" if blank."""
    text = str(value if value is not None else "").strip().title()
    return text if text in RESULT_VALUES else ""


def payout_units(result: str, odds) -> float:
    """Units returned on a 1-unit risk, ignoring the sheet's Units Risked."""
    if result == "Loss":
        return -1.0
    if result == "Win":
        odds_num = pd.to_numeric(str(odds).strip(), errors="coerce")
        if pd.isna(odds_num) or odds_num == 0:
            return 0.0
        return odds_num / 100 if odds_num > 0 else 100 / abs(odds_num)
    return 0.0


def build_results_html(completed_df: pd.DataFrame) -> str:
    if completed_df.empty:
        return ""
    wins = int((completed_df["_result"] == "Win").sum())
    losses = int((completed_df["_result"] == "Loss").sum())
    pushes = int((completed_df["_result"] == "Push").sum())
    total_units = completed_df["_payout"].sum()
    pct = total_units / len(completed_df) * 100
    sign_class = "pos" if total_units > 0 else "neg" if total_units < 0 else ""

    rows_html = ""
    for _, row in completed_df.iterrows():
        result = row["_result"]
        line = str(row.get("Line", "")).strip()
        pick_text = str(row.get("Pick", "")).strip()
        if line:
            pick_text += f" {line}"
        rows_html += "".join([
            '<div class="bb-result-row">',
            f'<div>{row.get("Away Team", "")} at {row.get("Home Team", "")}</div>',
            f'<div class="bb-result-type">{row.get("Bet Type", "")}</div>',
            f'<div class="bb-result-pick">{pick_text}<span class="odds">{row.get("Odds", "")}</span></div>',
            f'<div class="bb-num">{str(row.get("Grade", "")).strip()}</div>',
            f'<div class="bb-num">{row.get("Edge %", "")}</div>',
            f'<div class="bb-result-tag {result.lower()}">{result}</div>',
            "</div>",
        ])

    return "".join([
        '<div class="bb-results">',
        "<h2>Completed picks</h2>",
        f'<div class="bb-results-summary">{wins}-{losses}-{pushes} &nbsp;&nbsp; '
        f'<span class="{sign_class}">{total_units:+.2f} units &nbsp; {pct:+.1f}%</span></div>',
        f'<div class="bb-result-list">{rows_html}</div>',
        "</div>",
    ])


def build_glossary_html(used_labels: list[str]) -> str:
    if not used_labels:
        return ""
    items_html = ""
    for label in used_labels:
        slug, qualifies, track_record = GLOSSARY[label]
        is_error = label == "Potential Sportsline Error"
        error_class = " error-item" if is_error else ""
        heading = "Check this row" if is_error else label
        items_html += "".join([
            f'<div class="bb-glossary-item{error_class}" id="{slug}">',
            f"<h3>{heading}</h3>",
            f'<div class="bb-qualifies">{qualifies}</div>',
            f'<div class="bb-track-record">{track_record}</div>',
            "</div>",
        ])
    return "".join([
        '<div class="bb-glossary">',
        "<h2>Why these picks are here</h2>",
        items_html,
        "</div>",
    ])


# The page title is drawn as part of the custom header below, not by
# st.title(), so the whole page renders in one visual language instead of
# Streamlit's default styling sitting on top of the custom markup.
_, col_button = st.columns([6, 1])
with col_button:
    if st.button("Refresh now"):
        load_best_bets_and_meta.clear()

df, modified_time_raw = load_best_bets_and_meta()

if df.empty:
    st.info("No bets found in the Best Bets tab.")
else:
    df["NFL Week"] = pd.to_numeric(df["NFL Week"], errors="coerce")
    df["_result"] = df["Result"].map(normalize_result) if "Result" in df.columns else ""
    df["_date"] = pd.to_datetime(df["Date"], format="%m/%d/%Y", errors="coerce")
    df["_edge_num"] = pd.to_numeric(df["Edge %"], errors="coerce")
    df["_score"] = df.apply(score_row, axis=1)
    df["_flags"] = df.apply(row_matched_flags, axis=1)
    df["_kickoff"] = df.apply(_kickoff_key, axis=1)

    # Only the current season counts, for both the grids and the tracker.
    df = df[df["_date"] >= SEASON_START].copy()

    # A pick is "displayed" if it ranked top 4 for its bet type within its
    # week. Ranking runs over every row in the week, graded or not, so a
    # graded pick drops out of the grid without a lower-ranked pick moving
    # up to replace it. That keeps the tracked picks identical to what the
    # dashboard actually showed.
    displayed_idx = []
    for _, wk in df.groupby("NFL Week"):
        for bet_type in SECTIONS:
            subset = wk[(wk["Bet Type"] == bet_type) & (wk["_score"] > 0)]
            subset = subset.sort_values(
                by=["_score", "_edge_num", "_kickoff"], ascending=[False, False, True]
            )
            displayed_idx.extend(subset.head(4).index)
    displayed_df = df.loc[displayed_idx]

    completed_df = displayed_df[displayed_df["_result"] != ""].copy()
    completed_df["_payout"] = completed_df.apply(
        lambda r: payout_units(r["_result"], r.get("Odds", "")), axis=1
    )
    completed_df = completed_df.sort_values(
        by=["NFL Week", "_kickoff", "Bet Type"], ascending=[False, True, True]
    )

    current_week = df["NFL Week"].max()
    # Top card grids only ever show displayed picks that have not been graded yet.
    week_df = displayed_df[
        (displayed_df["NFL Week"] == current_week) & (displayed_df["_result"] == "")
    ]
    week_df = week_df.drop(columns=[c for c in DROPPED_COLUMNS if c in week_df.columns])

    section_dfs = {
        name: week_df[week_df["Bet Type"] == name].sort_values(
            by=["_score", "_edge_num", "_kickoff"], ascending=[False, False, True]
        )
        for name in SECTIONS
    }

    # Part 4: single gold star across all displayed cards, excluding any
    # card carrying the red error chip.
    all_displayed = pd.concat(section_dfs.values()) if any(len(d) for d in section_dfs.values()) else pd.DataFrame()
    star_key = None
    if not all_displayed.empty:
        non_error = all_displayed[all_displayed["_edge_num"].abs() < ERROR_EDGE_THRESHOLD]
        if not non_error.empty:
            winner = non_error.sort_values(
                by=["_score", "_edge_num", "_kickoff"], ascending=[False, False, True]
            ).iloc[0]
            star_key = (winner["Away Team"], winner["Home Team"], winner["Bet Type"])

    modified_dt_utc = datetime.fromisoformat(modified_time_raw.replace("Z", "+00:00"))
    modified_dt_et = modified_dt_utc.astimezone(ZoneInfo("America/New_York"))
    now_et = datetime.now(ZoneInfo("America/New_York"))
    next_read_dt = next_wednesday_8pm_et(now_et)

    last_read_str = modified_dt_et.strftime("%m.%d.%y %-I:%M %p ET")
    next_read_str = next_read_dt.strftime("%m.%d.%y")

    week_label = f"Week {int(current_week)}" if pd.notna(current_week) else "No games this season yet"
    header_html = "".join([
        '<div class="bb-head">',
        '<div>',
        "<h1>Best Bets</h1>",
        f'<div class="bb-sub">{week_label}, top four per bet type, '
        "ranked by how the segment has actually performed</div>",
        "</div>",
        '<div class="bb-stamp">',
        f"<div>READ {last_read_str}</div>",
        f'<div class="next">NEXT {next_read_str}</div>',
        "</div>",
        "</div>",
    ])

    sections_html = "".join(
        build_section_html(name, section_dfs[name], star_key) for name in SECTIONS
    )

    used_labels: list[str] = []
    if not all_displayed.empty:
        for _, row in all_displayed.iterrows():
            for label in row["_flags"]:
                if label not in used_labels:
                    used_labels.append(label)
            if row["_edge_num"] is not None and abs(row["_edge_num"]) >= ERROR_EDGE_THRESHOLD:
                if "Potential Sportsline Error" not in used_labels:
                    used_labels.append("Potential Sportsline Error")

    results_html = build_results_html(completed_df)
    glossary_html = build_glossary_html(used_labels)

    st.markdown(
        f'<div class="bb-wrap">{header_html}{sections_html}</div>'
        f"{results_html}{glossary_html}",
        unsafe_allow_html=True,
    )
