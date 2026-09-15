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
    @import url('https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600;700;800&display=swap');

    html, body, [class*="css"], [class*="st-"], .stApp, .stApp * {
        font-family: 'Open Sans', sans-serif !important;
    }

    .stApp {
        background-color: #0e1117;
    }

    :root {
        --card: #171b23;
        --card-border: #262b36;
        --text: #e6e8eb;
        --muted: #8b93a1;
        --accent: #35c37a;
        --accent-dim: #1f6e46;
        --gold: #d9a441;
        --err: #e05252;
    }

    .bb-wrap { max-width: 900px; margin: 0 auto; }

    .bb-section { margin-top: 32px; }
    .bb-section h2 {
        font-size: 13px;
        text-transform: uppercase;
        letter-spacing: 1px;
        color: var(--muted);
        border-bottom: 1px solid var(--card-border);
        padding-bottom: 8px;
        margin-bottom: 14px;
    }
    .bb-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .bb-empty-state { color: #5c6472; font-size: 13px; font-style: italic; padding: 10px 2px; }

    .bb-card {
        background: var(--card);
        border: 1px solid var(--card-border);
        border-left: 4px solid var(--accent-dim);
        border-radius: 10px;
        padding: 14px 16px;
        position: relative;
    }
    .bb-card.double { border-left-color: var(--gold); }
    .bb-card.flagged { border-left-color: var(--err); }
    .bb-card.starred { border-left-color: var(--gold); box-shadow: 0 0 0 1px rgba(217,164,65,0.35); }

    .bb-star-tag {
        position: absolute;
        top: 10px;
        right: 12px;
        font-size: 10px;
        font-weight: 700;
        color: var(--gold);
    }

    .bb-matchup { font-size: 13px; font-weight: 600; margin-bottom: 1px; padding-right: 70px; color: var(--text); }
    .bb-meta { font-size: 11px; color: var(--muted); margin-bottom: 8px; }

    .bb-pick-row { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }
    .bb-pick-side { font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); }
    .bb-pick-number { font-size: 19px; font-weight: 800; color: var(--text); }
    .bb-pick-odds { font-size: 11px; color: var(--muted); }

    .bb-stats { display: flex; gap: 12px; margin-bottom: 8px; }
    .bb-stat { text-align: left; }
    .bb-stat-label { font-size: 9px; color: var(--muted); text-transform: uppercase; }
    .bb-stat-value { font-size: 13px; font-weight: 700; color: var(--text); }
    .bb-grade-b { color: var(--accent); }
    .bb-grade-a { color: var(--gold); }

    .bb-badges { display: flex; gap: 5px; flex-wrap: wrap; }
    a.bb-badge {
        font-size: 9px;
        padding: 2px 7px;
        border-radius: 20px;
        font-weight: 600;
        text-decoration: none;
        cursor: pointer;
    }
    a.bb-badge.gold { background: rgba(217, 164, 65, 0.12); color: var(--gold); border: 1px solid rgba(217, 164, 65, 0.4); }
    a.bb-badge.warn { background: rgba(224, 76, 76, 0.14); color: var(--err); border: 1px solid rgba(224, 76, 76, 0.5); }
    a.bb-badge:hover { filter: brightness(1.25); }

    .bb-results { max-width: 900px; margin: 44px auto 0 auto; border-top: 1px solid var(--card-border); padding-top: 24px; }
    .bb-results h2 { font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
    .bb-results-summary { font-size: 14px; font-weight: 700; color: var(--text); margin-bottom: 12px; }
    .bb-results-summary .pos { color: var(--accent); }
    .bb-results-summary .neg { color: var(--err); }
    .bb-result-row {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 8px 12px;
        border-bottom: 1px solid var(--card-border);
        font-size: 12px;
        color: var(--text);
    }
    .bb-result-row:last-child { border-bottom: none; }
    .bb-result-matchup { flex: 2.2; font-weight: 600; min-width: 0; }
    .bb-result-type { flex: 0.9; color: var(--muted); }
    .bb-result-pick { flex: 1.6; }
    .bb-result-pick .odds { color: var(--muted); margin-left: 4px; }
    .bb-result-stat { flex: 0.6; color: var(--muted); }
    .bb-result-stat b { color: var(--text); font-weight: 700; }
    .bb-result-tag { flex: 0 0 48px; text-align: center; font-size: 10px; font-weight: 700; padding: 2px 0; border-radius: 20px; }
    .bb-result-tag.win { background: rgba(53, 195, 122, 0.14); color: var(--accent); border: 1px solid rgba(53, 195, 122, 0.5); }
    .bb-result-tag.loss { background: rgba(224, 76, 76, 0.14); color: var(--err); border: 1px solid rgba(224, 76, 76, 0.5); }
    .bb-result-tag.push { background: rgba(139, 147, 161, 0.14); color: var(--muted); border: 1px solid rgba(139, 147, 161, 0.5); }
    .bb-result-list { background: var(--card); border: 1px solid var(--card-border); border-radius: 10px; }

    .bb-glossary { max-width: 900px; margin: 44px auto 0 auto; border-top: 1px solid var(--card-border); padding-top: 24px; }
    .bb-glossary h2 { font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 14px; }
    .bb-glossary-item { background: var(--card); border: 1px solid var(--card-border); border-radius: 10px; padding: 16px 20px; margin-bottom: 12px; scroll-margin-top: 20px; }
    .bb-glossary-item.error-item { border-color: rgba(224, 76, 76, 0.35); }
    .bb-glossary-item h3 { font-size: 14px; margin: 0 0 6px 0; color: var(--text); }
    .bb-glossary-item .bb-qualifies { font-size: 13px; color: var(--muted); margin-bottom: 8px; line-height: 1.5; }
    .bb-glossary-item .bb-track-record { font-size: 13px; color: var(--accent); font-weight: 600; }
    .bb-glossary-item.error-item .bb-track-record { color: var(--err); }
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


def build_card_html(row: pd.Series, is_starred: bool) -> str:
    is_error = row["_edge_num"] is not None and abs(row["_edge_num"]) >= ERROR_EDGE_THRESHOLD

    card_classes = "bb-card"
    if is_starred:
        card_classes += " starred"
    elif row["_flags"]:
        card_classes += " double"
    if is_error:
        card_classes += " flagged"

    badges_html = ""
    for label in row["_flags"]:
        slug = GLOSSARY[label][0]
        badges_html += f'<a href="#{slug}" class="bb-badge gold">{label}</a>'
    if is_error:
        slug = GLOSSARY["Potential Sportsline Error"][0]
        badges_html += f'<a href="#{slug}" class="bb-badge warn">⚠ Error?</a>'

    grade = str(row.get("Grade", "")).strip()
    grade_class = "bb-grade-a" if grade.upper() == "A" else "bb-grade-b" if grade.upper() == "B" else ""
    sim_prob = row.get("Sim Probability", "")
    edge_display = row.get("Edge %", "")
    pick_side, pick_number, pick_odds = _pick_display(row)

    extra_fields = ""
    dome = str(row.get("Dome (Y/N)", "")).strip()
    temp = str(row.get("Temperature (F)", "")).strip()
    if dome:
        extra_fields += f'<div class="bb-stat"><div class="bb-stat-label">Dome</div><div class="bb-stat-value">{dome}</div></div>'
    if temp:
        extra_fields += f'<div class="bb-stat"><div class="bb-stat-label">Temp</div><div class="bb-stat-value">{temp}°F</div></div>'

    star_tag = '<div class="bb-star-tag">&#9733; LOVE THIS ONE</div>' if is_starred else ""

    lines = [
        f'<div class="{card_classes}">',
        star_tag,
        f'<div class="bb-matchup">{row.get("Away Team", "")} @ {row.get("Home Team", "")}</div>',
        f'<div class="bb-meta">{_meta_display(row)}</div>',
        '<div class="bb-pick-row">',
        f'<span class="bb-pick-side">{pick_side}</span>',
        f'<span class="bb-pick-number">{pick_number}</span>',
        f'<span class="bb-pick-odds">{pick_odds}</span>',
        "</div>",
        '<div class="bb-stats">',
        f'<div class="bb-stat"><div class="bb-stat-label">Grade</div><div class="bb-stat-value {grade_class}">{grade}</div></div>',
        f'<div class="bb-stat"><div class="bb-stat-label">Sim</div><div class="bb-stat-value">{sim_prob}%</div></div>',
        f'<div class="bb-stat"><div class="bb-stat-label">Edge</div><div class="bb-stat-value">{edge_display}%</div></div>',
        extra_fields,
        "</div>",
        f'<div class="bb-badges">{badges_html}</div>',
        "</div>",
    ]
    return "".join(lines)


def build_section_html(section_name: str, section_df: pd.DataFrame, star_key) -> str:
    parts = [
        '<div class="bb-section">',
        f"<h2>{section_name}</h2>",
    ]
    if section_df.empty:
        message = f"No qualifying {section_name} bets this week."
        if section_name == "Spread":
            message += (
                " No validated Spread segment exists yet, every Spread pattern "
                "tested so far failed the season-by-season re-check."
            )
        parts.append(f'<div class="bb-empty-state">{message}</div>')
    else:
        parts.append('<div class="bb-grid">')
        for _, row in section_df.iterrows():
            row_key = (row["Away Team"], row["Home Team"], row["Bet Type"])
            parts.append(build_card_html(row, is_starred=(row_key == star_key)))
        parts.append("</div>")
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
            f'<div class="bb-result-matchup">{row.get("Away Team", "")} @ {row.get("Home Team", "")}</div>',
            f'<div class="bb-result-type">{row.get("Bet Type", "")}</div>',
            f'<div class="bb-result-pick">{pick_text}<span class="odds">{row.get("Odds", "")}</span></div>',
            f'<div class="bb-result-stat">Grade <b>{str(row.get("Grade", "")).strip()}</b></div>',
            f'<div class="bb-result-stat">Edge <b>{row.get("Edge %", "")}%</b></div>',
            f'<div class="bb-result-tag {result.lower()}">{result}</div>',
            "</div>",
        ])

    return "".join([
        '<div class="bb-results">',
        "<h2>Completed Picks</h2>",
        f'<div class="bb-results-summary">Record: {wins}-{losses}-{pushes} (W-L-P) &nbsp;·&nbsp; '
        f'Return: <span class="{sign_class}">{total_units:+.2f} units ({pct:+.1f}%)</span></div>',
        f'<div class="bb-result-list">{rows_html}</div>',
        "</div>",
    ])


def build_glossary_html(used_labels: list[str]) -> str:
    if not used_labels:
        return ""
    items_html = ""
    for label in used_labels:
        slug, qualifies, track_record = GLOSSARY[label]
        error_class = " error-item" if label == "Potential Sportsline Error" else ""
        heading = f"⚠ {label}" if label == "Potential Sportsline Error" else label
        items_html += "".join([
            f'<div class="bb-glossary-item{error_class}" id="{slug}">',
            f"<h3>{heading}</h3>",
            f'<div class="bb-qualifies">{qualifies}</div>',
            f'<div class="bb-track-record">{track_record}</div>',
            "</div>",
        ])
    return "".join([
        '<div class="bb-glossary">',
        "<h2>Why These Picks Are Here</h2>",
        items_html,
        "</div>",
    ])


st.title("Best Bets")

col_title, col_button = st.columns([5, 1])
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

    last_read_str = modified_dt_et.strftime("%a, %b %-d, %Y · %-I:%M %p ET")
    next_read_str = next_read_dt.strftime("%a, %b %-d, %Y")

    week_label = f"NFL Week {int(current_week)}" if pd.notna(current_week) else "No games this season yet"
    st.caption(f"{week_label} · top 4 per bet type · ranked by historical segment strength")
    st.markdown(
        f'<p style="margin-top: -8px; color: #5c6472; font-size: 13px;">'
        f"Last Read: {last_read_str} &nbsp;|&nbsp; Next Read: {next_read_str}</p>",
        unsafe_allow_html=True,
    )

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
        f'<div class="bb-wrap">{sections_html}</div>{results_html}{glossary_html}',
        unsafe_allow_html=True,
    )
