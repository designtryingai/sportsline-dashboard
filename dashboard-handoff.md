# Handoff to Claude Code: Best Bets Dashboard v3 (grid layout)

This **replaces every earlier dashboard spec in full**, including
`dashboard-handoff.md` v1 and v2. Don't merge with those, this is the
single complete spec. `~/Desktop/sportsline-automation/dashboard/` already
exists with a working `dashboard_app.py` live on Streamlit Community Cloud.
This is an update to that same app and repo, not a new build. Once pushed
to GitHub, Streamlit Cloud auto-redeploys, no separate deploy step needed.

A static HTML mockup (`dashboard-mockup.html`) shows the exact approved
target look: dark theme, Open Sans, three horizontal sections (Spread,
Total, Moneyline, in that order top to bottom), each holding up to a 2x2
grid of compact cards. Open it in a browser before starting.

Still true, unchanged from earlier rounds:
- Read-only. Never write to the Sheet.
- Credentials load only from `st.secrets`, never from a committed file.
- `.gitignore` excludes all credential files.

---

## Part 1: Filter to the active week

Read the full "Best Bets" tab. Filter to only rows where NFL Week equals
the highest NFL Week value present in the tab. This is the same rule as
before, unchanged.

---

## Part 2: Score every remaining row

Each row's Profitable Segment Match column lists one or more flag names.
Score = sum of the weights below for every flag matched. For the
grade-based flag, use the grade-specific weight based on that row's actual
Grade value, not a blended number.

| Flag | Weight | Source |
|---|---|---|
| Home Favorite Moneyline | 22.9 | Since 2024: 98 bets, 77.6% win, +22.9% ROI |
| Warm Outdoor Total | 15.0 | All 3 seasons (2023-2025): 207 bets, 60.5% win, +15.0% ROI. No since-2024-only figure exists (needs a weather proxy not in the live data). Has never fired on real data, since Dome/Temperature aren't scraped yet. |
| Total, Graded B | 11.5 | Since 2024: 111 bets, 58.6% win, +11.5% ROI |
| Total, Big Edge | 9.1 | Since 2024: 217 bets, 57.1% win, +9.1% ROI |
| Total, Medium Edge | 5.2 | Since 2024: 88 bets, 54.5% win, +5.2% ROI |
| Total, Graded A | 2.1 | Since 2024: 15 bets, 53.3% win, +2.1% ROI. Small sample, weakest signal. |

---

## Part 3: Split into three sections, top 4 each

**This supersedes any earlier "top 5 overall" rule.** Instead of one pooled
ranking, rank separately within each Bet Type:

1. Split the scored rows into three groups by Bet Type: Spread, Total,
   Moneyline.
2. Within each group, sort by score descending, take the top 4.
3. Tie-break, same rule in all three groups: higher Edge % first, then
   earliest kickoff date/time if still tied.
4. **If a group has fewer than 4 qualifying rows, show only what exists.
   Never pad with unflagged or lower-scoring rows to reach 4.**
5. Spread currently has no flags defined at all (every Spread pattern
   tested failed the season-by-season re-check, see the segment analysis).
   That means the Spread group will be empty every week under the current
   five-flag rule set. This is expected, not a bug, don't special-case it
   or invent a Spread rule to fill the section.

**Layout:** three sections stacked vertically, in this order: Spread on
top, then Total, then Moneyline. Each section has its own header. Cards
within a section render in a 2-column grid (2x2 when a section has 4
cards, fewer rows/cards if fewer qualify).

**Empty section state:** when a section has zero qualifying rows, show the
section header with a plain muted-text message in place of any cards, no
placeholder cards, no "N/A" grid. Wording per the mockup: *"No qualifying
[Bet Type] bets this week."* For Spread specifically, add the reason too,
matching the mockup exactly: *"No validated Spread segment exists yet,
every Spread pattern tested so far failed the season-by-season re-check."*

---

## Part 4: The gold star, "Love This One"

**One single star total, across all displayed cards in all three
sections, not one per section.**

Algorithm:
1. Look at every card actually displayed this week (across Spread, Total,
   and Moneyline combined, so up to 12 cards, fewer if any section came up
   short).
2. Exclude any card carrying the red "Potential Sportsline Error" chip
   (Part 6).
3. Among what's left, find the single highest score.
4. Tie-break the same way as Part 3: higher Edge % first, then earliest
   kickoff date/time.
5. That one card gets a gold star and the label "Love This One" (styled
   per the mockup: gold left border, gold ring/glow, star badge in the
   corner).
6. **Edge case:** if every displayed card that week happens to carry the
   error chip, don't force a star onto an error-flagged card. No star that
   week. Never guess a substitute.

---

## Part 5: Fields per card, and what got dropped

Card fields, per the mockup: matchup (Away @ Home), date/time, the pick
(Over/Under + line for Totals, team name for Moneyline, will need a
sensible format for Spread once that section can ever populate), odds,
Grade, Sim Probability, Edge %, and badge(s) for matched flags.

Dropped entirely, unchanged from the earlier round: Spread Movement
Direction, Units Risked, Result, Units Won/Lost (always blank right now,
unused until Phase 6.6 is live), and Confidence (identical to Grade,
carries no separate information, per Brandon's own segment analysis).

Open and Current (market line) are still not shown on the card face, same
as the prior approved design. Not explicitly requested removed, just never
made it into any iteration. Leave it out unless it's specifically asked
for later.

Dome and Temperature: only render if they actually have a value (they're
blank on every row today), never show an empty field.

---

## Part 6: Badges, glossary, and the error chip

**Badges are clickable**, each one is an `<a href="#glossary-slug">` link
down to a matching entry in a "Why These Picks Are Here" section at the
very bottom of the page, below all three sections.

**Grade-specific labeling rule still applies:** a Grade B row's badge
reads "Graded B," a Grade A row's reads "Graded A." Never a generic
"Graded B/A" on an actual card.

**Glossary is dynamic:** only include an entry for a flag type if at least
one currently-displayed card actually carries that badge. Use the exact
bets/win%/ROI numbers from the Part 2 table, don't restate from memory.

**Red error chip:** any displayed card where `abs(Edge %) >= 20` gets an
extra red badge reading "⚠ Potential Sportsline Error" (mockup uses the
shorter "⚠ Error?" on the compact card itself, full text in the glossary
entry it links to). This does not change the row's score or its place in
the top-4 ranking for its section, and per Part 4 it blocks that card from
ever getting the gold star. It gets its own glossary entry, framed as a
data-quality caution rather than a proven segment, exact wording in the
mockup.

**Technical requirement, unchanged from before:** render the sections,
cards, and glossary as one continuous block in the normal page flow
(`st.markdown(..., unsafe_allow_html=True)`), not through
`st.components.v1.html`, since that sandboxes into an iframe and breaks
the anchor-jump from badge to glossary entry. Test the click-and-scroll
behavior before calling this done.

---

## Part 7: "Last Read" / "Next Read" line

Unchanged from the prior round. Below the subtitle line: **Last Read**
pulled from the real Google Sheet file's last-modified timestamp via the
Drive API (`files().get(fileId=..., fields='modifiedTime')`), needs
`google-api-python-client` added to `requirements.txt` if it isn't there
yet, plus the `drive.metadata.readonly` scope alongside the existing
spreadsheets-readonly scope. Still fully read-only. **Next Read** is a
calendar projection to the next upcoming Wednesday at 8:00 PM ET, labeled
clearly as a projection, not presented as a fact.

---

## Part 8: Testing before handing back to Brandon

1. Run locally. Confirm this week's actual output against a hand-check:
   Spread section empty with the exact wording above; Total top 4 (tied at
   score 20.6, tie-broken by Edge %) should be NY Jets/Tennessee (flagged,
   27.8% edge), Cleveland/Jacksonville, Denver/Kansas City, Dallas/NY
   Giants, in that order, with New Orleans/Detroit correctly excluded;
   Moneyline top 4 (tied at score 22.9) should be New England/Seattle,
   Buffalo/Houston, Green Bay/Minnesota, Atlanta/Pittsburgh, in that order.
2. Confirm the gold star lands on New England @ Seattle specifically (wins
   the Edge % tie against Buffalo/Houston by having the earlier kickoff),
   and confirm no other card has a star.
3. Confirm every badge on every card is clickable and scrolls correctly to
   its glossary entry, including the new Home Favorite ML entry.
4. Confirm the red error chip appears only on the NY Jets/Tennessee Total
   card this week, and nowhere else.
5. Confirm Last Read shows a real current timestamp from the Drive API and
   Next Read shows the correct upcoming Wednesday.
6. Print or describe the full rendered output to Brandon for review before
   pushing to GitHub.

---

## Checklist

- [ ] Filter to active week (max NFL Week)
- [ ] Scoring formula, six weights, grade-aware
- [ ] Split into Spread / Total / Moneyline groups, ranked separately
- [ ] Top 4 per group with tie-break rules (Edge %, then earliest kickoff)
- [ ] Fewer-than-4 case handled per group without padding
- [ ] Section order: Spread, then Total, then Moneyline, 2x2 grid layout
- [ ] Empty-section state with exact wording, including the Spread-specific
      reason
- [ ] Single gold star across all sections, exclusion of error-flagged
      cards, tie-break rule, no-star edge case handled
- [ ] Spread Movement, Units Risked, Result, Units Won/Lost, Confidence all
      dropped from cards
- [ ] Grade-specific badge labels, never generic "B/A"
- [ ] Badges are real anchor links, glossary built dynamically
- [ ] Glossary numbers copied exactly from the Part 2 table
- [ ] Cards + glossary in normal page flow, not an iframe component,
      anchor jump tested
- [ ] Red error chip logic, independent of scoring, blocks the gold star
- [ ] Error chip has its own glossary entry
- [ ] Last Read from real Drive API timestamp, new scope and dependency
      added
- [ ] Next Read as a clearly-labeled projection
- [ ] Local test run matching the exact hand-checked output in Part 8,
      reviewed by Brandon before pushing to GitHub
