"""Small Streamlit helpers shared by both apps (presentation only)."""

from __future__ import annotations

import streamlit as st

from dashboard.http_core import Problem

HINTS = {
    "unauthenticated": "The token was rejected or is missing. Set the token environment variable for this app and restart it.",
    "forbidden": "This token's role is not allowed to read this data.",
    "unreachable": "The API could not be reached. Check that it is running and that DASHBOARD_API_BASE_URL is correct.",
    "timeout": "The API took too long to answer. Try Refresh; nothing was changed.",
    "unavailable": "The API reports this capability as unavailable in its current configuration.",
    "rate_limited": "Too many failed authentication attempts. Wait before retrying.",
    "bad_response": "The API answered with something unexpected.",
}

# colour per meaning; every badge also carries its text, so colour is never the only signal
SEVERITY_BADGE = {"critical": "red", "high": "orange", "medium": "yellow", "low": "blue"}
STATUS_BADGE = {"open": "red", "new": "blue", "investigating": "orange", "awaiting_review": "violet", "awaiting_approval": "violet",
                "resolved": "green", "closed": "gray", "recovery_completed": "green", "recovery_uncertain": "red",
                "recovery_failed": "red", "recovery_not_executed": "gray", "pending": "violet", "approved": "green",
                "rejected": "gray", "expired": "gray"}

_CSS = """
<style>
:root {--navy:#182438; --ink:#1B2A3D; --muted:#51607A; --line:#C9D3E1; --blue:#3977D5; --green:#2B9B78; --amber:#D49B32; --red:#C94A4A;
  --heading: Bahnschrift, "DIN Alternate", "Segoe UI", system-ui, sans-serif;}
html, body, [data-testid="stAppViewContainer"] {font-family: "Segoe UI", system-ui, -apple-system, sans-serif;}
.block-container {padding-top: 1.6rem; max-width: 1400px;}
h1, h2, h3, .page-head h1 {font-family: var(--heading); letter-spacing: .005em; color: var(--ink);}
/* page header */
.page-head {border-bottom: 2px solid var(--navy); padding-bottom: .55rem; margin-bottom: 1rem;}
.page-head h1 {font-size: 1.7rem; margin: 0; padding: 0;}
.page-sub {color: var(--muted); margin: .2rem 0 0; max-width: 70ch; line-height: 1.4;}
.page-note {display: block; color: var(--muted); font-size: .82rem; margin-top: .3rem;}
/* KPI strip: metrics read as one connected strip with a left rule, not separate cards */
[data-testid="stMetric"] {background: #fff; border: 1px solid var(--line); border-left: 4px solid var(--blue); border-radius: 2px; padding: .65rem .9rem;}
[data-testid="stMetricValue"] {font-variant-numeric: tabular-nums; font-family: var(--heading);}
[data-testid="stMetricLabel"] p {color: var(--muted);}
/* status pills: glyph + text */
.pill {display: inline-block; padding: .05rem .5rem; border-radius: 999px; font-size: .82rem; font-weight: 600; border: 1px solid currentColor; white-space: nowrap;}
.pill-ok {color: #1D6F55; background: #E4F4EE;} .pill-warn {color: #7A5410; background: #FBF0D9;}
.pill-bad {color: #9B2F2F; background: #FBE6E6;} .pill-info {color: #1F58A8; background: #E3EDFB;}
.pill-hold {color: #4B3C8F; background: #ECE8FA;} .pill-neutral {color: #3F4C63; background: #EEF1F6;}
/* certainty device: solid = observed, dashed = AI hypothesis, hatched = unavailable */
.fact, .hypo, .unavailable, .empty-state {background: #fff; padding: .7rem 1rem; margin: .6rem 0; border-radius: 2px;}
.fact {border: 1px solid var(--line); border-left: 5px solid var(--navy);}
.hypo {border: 2px dashed #7C8CA6; border-left: 5px dashed var(--blue);}
.unavailable {border: 1px solid var(--line); background: repeating-linear-gradient(135deg, #fff 0 9px, #EEF1F6 9px 11px);}
.empty-state {border: 1px dashed var(--line); color: var(--muted);}
.fact h3, .hypo h3 {margin: 0; font-size: 1.05rem;}
.kind {color: var(--muted); font-size: .82rem; margin: .1rem 0 .3rem;}
.fact ul, .hypo ul {margin: .2rem 0 0; padding-left: 1.2rem;}
.demo-banner {border-left: .35rem solid #7c3aed; background: rgba(124,58,237,.10); padding: .5rem .8rem; border-radius: .3rem; margin-bottom: .6rem;}
[data-testid="stSidebar"] h1 {font-family: var(--heading); font-size: 1.3rem;}
a:focus-visible, button:focus-visible, input:focus-visible, [role="radio"]:focus-visible, [data-baseweb="select"]:focus-within {outline: 3px solid #F2B84B !important; outline-offset: 2px;}
@media (max-width: 640px) {.block-container {padding-left: .8rem; padding-right: .8rem;} .page-head h1 {font-size: 1.35rem;}}
@media (prefers-reduced-motion: reduce) {* {transition: none !important; animation: none !important;}}
</style>
"""


def apply_theme() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def badge(kind: str, value: object) -> str:
    """Markdown badge, e.g. ``:red-badge[critical]``. Falls back to plain text for an unknown value."""
    text = str(value if value not in (None, "") else "unknown").replace("[", "(").replace("]", ")")
    colour = (SEVERITY_BADGE if kind == "severity" else STATUS_BADGE).get(str(value))
    return f":{colour}-badge[{text}]" if colour else f":gray-badge[{text}]"


def demo_banner(text: str = "SYNTHETIC DEMONSTRATION DATA - not real incidents, not stored in any real system.") -> None:
    st.markdown(f'<div class="demo-banner" role="note"><strong>DEMO</strong> &nbsp;{text}</div>', unsafe_allow_html=True)


def nav_links(*, to_ledger: str | None = None, to_ops: str | None = None) -> None:
    """Links to the sibling app, in the sidebar. A deep link carries only identifiers, never a token."""
    if to_ledger:
        st.sidebar.link_button("Open VerbaLedger Explorer", to_ledger, width="stretch", icon=":material/fact_check:")
    if to_ops:
        st.sidebar.link_button("Open Operations Dashboard", to_ops, width="stretch", icon=":material/monitoring:")


def show_problem(problem: Problem, what: str) -> None:
    extra = f" (HTTP {problem.status}, code {problem.code}, request {problem.request_id})" if problem.status else ""
    st.error(f"Could not load {what}: {problem.message}{extra}")
    hint = HINTS.get(problem.kind)
    if hint:
        st.caption(hint)


def show_recovery_guidance(guide: dict) -> None:
    """The honest 'this capability is off, here is exactly why and how to enable it' state. Not an error to hide, not a
    working page: the queue is not drawn and no count is shown."""
    st.warning(f"**{guide['title']}.** {guide['message']}")
    bits = [f"reason: {guide['reason']}" if guide.get("reason") else None,
            f"database: {guide['database']}" if guide.get("database") else None,
            f"request {guide['request_id']}" if guide.get("request_id") else None]
    st.caption("API said - " + "; ".join(b for b in bits if b) + ". Nothing is pending or decided here until this is fixed.")
    if guide.get("steps"):
        with st.expander("How to enable Human Review and recovery (Windows PowerShell)", expanded=True):
            st.markdown("The safety guard is deliberate and stays on: recovery is only allowed to write to a database whose name ends with "
                        "`_test`, never to `dataops_sim`. Use a separate demo database:")
            st.code("\n".join(guide["steps"]), language="powershell")
    elif guide.get("fix"):
        st.info(guide["fix"])


def show_chart(container, fig, empty_message: str) -> None:
    """Draw a chart, or an explanatory note when there is nothing to draw. A real statement on purpose: a bare conditional
    expression would be 'magic'-rendered by Streamlit as the text of a DeltaGenerator object."""
    if fig:
        container.plotly_chart(fig, width="stretch")
    else:
        container.info(empty_message)


def tone(kind: str, text: str) -> None:
    {"success": st.success, "warning": st.warning, "error": st.error}.get(kind, st.info)(text)


def pager(total: int, page: int, page_count: int, noun: str) -> None:
    st.caption(f"{total} {noun} match - page {page} of {page_count}")
