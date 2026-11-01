"""Reusable presentation pieces for the Operations Dashboard (no data access, no side effects).

The visual rule used throughout: a SOLID rule marks observed fact, a DASHED rule marks model-generated analysis, a
HATCHED panel marks data that is unavailable. Status is always glyph plus text, never colour alone.
"""

from __future__ import annotations

from html import escape

import streamlit as st

# status -> (glyph, css class). The glyph is part of the text so colour is never the only signal.
_STATUS = {
    "success": ("✓", "ok"), "ok": ("✓", "ok"), "resolved": ("✓", "ok"), "approved": ("✓", "ok"),
    "pass": ("✓", "ok"), "warn": ("▲", "warn"), "fail": ("✕", "bad"), "warning": ("▲", "warn"), "medium": ("▲", "warn"), "high": ("▲", "warn"), "investigating": ("▲", "warn"),
    "failed": ("✕", "bad"), "critical": ("✕", "bad"), "open": ("✕", "bad"), "error": ("✕", "bad"),
    "running": ("◌", "info"), "new": ("◌", "info"), "low": ("◌", "info"),
    "pending": ("⏸", "hold"), "awaiting_review": ("⏸", "hold"), "awaiting_approval": ("⏸", "hold"),
}


def pill(value: object) -> str:
    """HTML status pill, e.g. '✕ failed'. Unknown values render as a neutral pill with the raw text."""
    text = str(value if value not in (None, "") else "unknown")
    glyph, cls = _STATUS.get(text.lower(), ("·", "neutral"))
    return f'<span class="pill pill-{cls}">{glyph}&nbsp;{escape(text.replace("_", " "))}</span>'


def page_header(title: str, subtitle: str = "", *, loaded_note: str = "") -> None:
    """Consistent page header: title, one plain sentence of purpose, and when the data was loaded."""
    note = f'<span class="page-note">{escape(loaded_note)}</span>' if loaded_note else ""
    sub = f'<p class="page-sub">{escape(subtitle)}</p>' if subtitle else ""
    st.markdown(f'<header class="page-head"><h1>{escape(title)}</h1>{sub}{note}</header>', unsafe_allow_html=True)


def unavailable(title: str, reason: str, *, how_to_fix: str = "") -> None:
    """Hatched panel for a capability or metric the API cannot provide. States why; never shows a number."""
    fix = f"<p>{escape(how_to_fix)}</p>" if how_to_fix else ""
    st.markdown(f'<section class="unavailable" role="note"><strong>{escape(title)}</strong><p>{escape(reason)}</p>{fix}</section>',
                unsafe_allow_html=True)


def fact_block(heading: str, lines: list[str]) -> None:
    """Observed facts: solid rule."""
    items = "".join(f"<li>{escape(str(s))}</li>" for s in lines) or "<li>none recorded</li>"
    st.markdown(f'<section class="fact" aria-label="{escape(heading)}"><h3>{escape(heading)}</h3>'
                f'<p class="kind">Observed from collected evidence</p><ul>{items}</ul></section>', unsafe_allow_html=True)


def hypothesis_block(heading: str, lines: list[str]) -> None:
    """Model-generated, unconfirmed analysis: dashed rule."""
    items = "".join(f"<li>{escape(str(s))}</li>" for s in lines) or "<li>none recorded</li>"
    st.markdown(f'<section class="hypo" aria-label="{escape(heading)}"><h3>{escape(heading)}</h3>'
                f'<p class="kind">AI-generated, not confirmed</p><ul>{items}</ul></section>', unsafe_allow_html=True)


def empty_state(message: str, next_step: str = "") -> None:
    extra = f"<p>{escape(next_step)}</p>" if next_step else ""
    st.markdown(f'<section class="empty-state"><p>{escape(message)}</p>{extra}</section>', unsafe_allow_html=True)


def nav_group_css(starts: dict[int, str]) -> None:
    """Group headings above chosen sidebar radio options (1-based position -> heading). Pure CSS: if Streamlit's markup
    changes the headings disappear and the list is simply ungrouped; navigation itself is unaffected."""
    rules = "".join(
        f'[data-testid="stSidebar"] [role="radiogroup"] > div:nth-child({n})::before'
        f'{{content:"{escape(t)}"; display:block; width:100%; color:#9FB0CB; font-size:.78rem; font-weight:600; '
        f'margin:{".2rem" if n == 1 else ".9rem"} 0 .25rem; letter-spacing:.02em;}}'
        for n, t in starts.items())
    st.markdown(f"<style>{rules}</style>", unsafe_allow_html=True)
