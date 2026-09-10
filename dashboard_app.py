"""Public read-only dashboard for the Best Bets tab of the Sportsbook Model sheet.

This app never writes to the Google Sheet. It only reads the "Best Bets"
worksheet and displays the current week's rows.
"""

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

SHEET_ID = "1nEtrFPgESIzuBXf3bGSxVbZ1kUJyFx6-xGKjPJVqthM"
TAB_NAME = "Best Bets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

st.set_page_config(page_title="Best Bets", layout="wide")

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600;700&display=swap');

    html, body, [class*="css"], [class*="st-"], .stApp, .stApp * {
        font-family: 'Open Sans', sans-serif !important;
    }

    .stApp {
        background-color: #0e1117;
    }

    thead tr th {
        font-weight: 600 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=600)
def load_best_bets() -> pd.DataFrame:
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    client = gspread.authorize(creds)
    ws = client.open_by_key(SHEET_ID).worksheet(TAB_NAME)
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame()
    return pd.DataFrame(values[1:], columns=values[0])


st.title("Best Bets")

col_title, col_button = st.columns([5, 1])
with col_button:
    if st.button("Refresh now"):
        load_best_bets.clear()

df = load_best_bets()

if df.empty:
    st.info("No bets found in the Best Bets tab.")
else:
    df["NFL Week"] = pd.to_numeric(df["NFL Week"], errors="coerce")
    current_week = df["NFL Week"].max()
    current = df[df["NFL Week"] == current_week].drop(columns=["NFL Week"])

    st.caption(f"NFL Week {int(current_week)} — {len(current)} bets")

    def highlight_multi_match(row):
        match_text = str(row.get("Profitable Segment Match", ""))
        is_multi = match_text.count(",") >= 1
        style = "background-color: #1c2b3a;" if is_multi else ""
        return [style] * len(row)

    styled = current.style.apply(highlight_multi_match, axis=1)
    st.dataframe(styled, width="stretch", hide_index=True)
