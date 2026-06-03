"""Streamlit dashboard — Brigade Road, Bangalore.

Business-narrative layout. Reads from the API HTTP contract.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pandas as pd
import streamlit as st

API_BASE  = os.environ.get("API_BASE", "http://api:8000")
REFRESH_S = int(os.environ.get("DASHBOARD_REFRESH_S", "5"))

st.set_page_config(
    page_title="Purplle — Brigade Road",
    page_icon="🛍️",
    layout="wide",
)

st.markdown(
    """
    <style>
      .block-container { padding-top: 1.2rem; }
      [data-testid="stMetricValue"] { font-size: 30px; }
      [data-testid="stMetricLabel"] { font-size: 12px; opacity: 0.7; text-transform: uppercase; }
      .funnel-bar  { background: linear-gradient(90deg, #7e22ce, #b22ba9); height: 22px; border-radius: 4px; }
      .funnel-row  { font-family: ui-monospace, monospace; padding: 6px 0; }
      .activity    { font-family: ui-monospace, monospace; font-size: 13px; padding: 2px 0; opacity: 0.9; }
      .anomaly     { background: #2b1f1f; border-left: 3px solid #f59e0b; padding: 8px 12px; margin: 4px 0; border-radius: 4px; }
      .anomaly.alert { background: #2b1717; border-left-color: #ef4444; }
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------


def _get(path: str, **params: Any) -> Any:
    try:
        r = httpx.get(f"{API_BASE}{path}", params=params, timeout=4.0)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _inr(n: float) -> str:
    """₹ formatting with thousand separators (₹ 1,07,881 — Indian numbering)."""
    n = int(round(n))
    if n < 1000:
        return f"₹ {n:,}"
    s = str(n)
    last3, rest = s[-3:], s[:-3]
    rest_grouped = ""
    while len(rest) > 2:
        rest_grouped = "," + rest[-2:] + rest_grouped
        rest = rest[:-2]
    return f"₹ {rest}{rest_grouped},{last3}"


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _fmt_time(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except Exception:
        return ts


# --------------------------------------------------------------------------
# Sidebar — small, focused
# --------------------------------------------------------------------------


with st.sidebar:
    st.markdown("### Store ST1008")
    st.caption("Brigade Road · Bangalore")
    st.divider()
    window = st.radio("Window", ["Last 24h", "Last 7d"], horizontal=True)
    hours  = 24 if window == "Last 24h" else 168
    auto   = st.toggle("Auto-refresh", value=True)
    st.caption(f"Refresh every {REFRESH_S}s")
    st.divider()
    ready = _get("/readyz")
    if isinstance(ready, dict) and ready.get("status") == "ready":
        st.success("System healthy")
    else:
        st.warning("Service degraded")
    st.caption(f"API: `{API_BASE}`")


# --------------------------------------------------------------------------
# Header + KPI strip
# --------------------------------------------------------------------------


metrics = _get("/metrics", hours=hours) or {}

footfall = int(metrics.get("footfall", 0))
unique   = int(metrics.get("unique_visitors", 0))
purchases = int(metrics.get("purchases", 0))
conv     = float(metrics.get("conversion_rate", 0.0)) * 100
avg_dwell = float(metrics.get("avg_session_duration_s", 0.0))
revenue   = float(metrics.get("revenue_inr", 0.0))
basket    = float(metrics.get("avg_basket_inr", 0.0))
items     = int(metrics.get("items_sold", 0))

st.title("Purplle · Brigade Road")
st.caption(f"Live retail intelligence — {window.lower()}")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Footfall",        f"{footfall:,}")
c2.metric("Conversion rate", f"{conv:.1f}%")
c3.metric("Revenue",         _inr(revenue))
c4.metric("Avg basket",      _inr(basket))

c5, c6, c7, c8 = st.columns(4)
c5.metric("Unique visitors", f"{unique:,}")
c6.metric("Purchases",       f"{purchases:,}")
c7.metric("Items sold",      f"{items:,}")
c8.metric("Avg dwell time",  _fmt_duration(avg_dwell))

st.divider()


# --------------------------------------------------------------------------
# Funnel waterfall
# --------------------------------------------------------------------------


st.subheader("Conversion funnel")

funnel = _get("/funnel", hours=hours) or {}
stages = funnel.get("stages", {}) if isinstance(funnel, dict) else {}
STAGE_LABELS = [
    ("entered",         "Entered the store"),
    ("browsed",         "Browsed a shelf"),
    ("engaged",         "Engaged (≥ 20 s dwell)"),
    ("checkout_queued", "Reached the counter"),
    ("purchased",       "Purchased"),
]

base = max(int(stages.get("entered", 0)), 1)
for stage_id, label in STAGE_LABELS:
    n = int(stages.get(stage_id, 0))
    pct = (n / base) * 100 if base else 0
    bar_pct = (n / base) * 100 if base else 0
    col_l, col_b, col_r = st.columns([3, 8, 2])
    col_l.markdown(f"**{label}**")
    col_b.markdown(
        f'<div style="background:#1f2937; border-radius:4px; overflow:hidden;">'
        f'<div class="funnel-bar" style="width:{bar_pct:.1f}%"></div></div>',
        unsafe_allow_html=True,
    )
    col_r.markdown(f"<span class='funnel-row'>{n:,} · {pct:.1f}%</span>", unsafe_allow_html=True)

st.divider()


# --------------------------------------------------------------------------
# Shelves + hourly trend
# --------------------------------------------------------------------------


left, right = st.columns([5, 5])

with left:
    st.subheader("Top performing shelves")
    zones = _get("/zones", hours=hours) or {}
    rows = zones.get("zones", []) if isinstance(zones, dict) else []
    if rows:
        df = pd.DataFrame(rows).head(8)
        df["Shelf"]    = df["zone_id"].str.replace("shelf_", "", regex=False).str.replace("_", " ").str.title()
        df["Visitors"] = df["unique_visitors"]
        df["Avg dwell"] = df["avg_dwell_s"].apply(lambda x: f"{x:.0f}s")
        st.dataframe(
            df[["Shelf", "Visitors", "Avg dwell"]],
            hide_index=True,
            use_container_width=True,
            height=320,
        )
    else:
        st.info("No shelf visits yet.")

with right:
    st.subheader("Hourly footfall & conversion")
    hourly = _get("/hourly", hours=hours) or {}
    hrows = hourly.get("hours", []) if isinstance(hourly, dict) else []
    if hrows:
        df_h = pd.DataFrame(hrows)
        df_h["hour_bucket"] = pd.to_datetime(df_h["hour_bucket"])
        df_h["Hour"] = df_h["hour_bucket"].dt.strftime("%H:00")
        df_h = df_h.rename(columns={"footfall": "Footfall", "purchases": "Purchases"})
        st.bar_chart(
            df_h.set_index("Hour")[["Footfall", "Purchases"]],
            height=300,
        )
    else:
        st.info("No hourly data yet.")

st.divider()


# --------------------------------------------------------------------------
# Sales breakdown — salespeople + payment modes
# --------------------------------------------------------------------------


st.subheader("Sales breakdown")
sales = _get("/sales", hours=hours) or {}
sp_rows   = sales.get("top_salespeople", []) if isinstance(sales, dict) else []
mode_rows = sales.get("payment_modes",   []) if isinstance(sales, dict) else []

sa, sb = st.columns([5, 5])

with sa:
    st.caption("Top salespeople (by revenue)")
    if sp_rows:
        df_sp = pd.DataFrame(sp_rows)
        df_sp["Salesperson"] = df_sp["salesperson"].astype(str)
        df_sp["Revenue"]     = df_sp["revenue"].apply(_inr)
        df_sp["Purchases"]   = df_sp["purchases"]
        df_sp["Items"]       = df_sp["items"]
        st.dataframe(
            df_sp[["Salesperson", "Revenue", "Purchases", "Items"]],
            hide_index=True,
            use_container_width=True,
            height=215,
        )
    else:
        st.info("No purchases yet.")

with sb:
    st.caption("Payment mode mix")
    if mode_rows:
        df_m = pd.DataFrame(mode_rows)
        df_m["Mode"]      = df_m["mode"]
        df_m["Revenue"]   = df_m["revenue"].apply(_inr)
        df_m["Purchases"] = df_m["purchases"]
        st.dataframe(
            df_m[["Mode", "Revenue", "Purchases"]],
            hide_index=True,
            use_container_width=True,
            height=215,
        )
    else:
        st.info("No payment data yet.")

st.divider()


# --------------------------------------------------------------------------
# Live activity + anomalies
# --------------------------------------------------------------------------


la, ra = st.columns([5, 5])

with la:
    st.subheader("Recent activity")
    act = _get("/activity", limit=15) or {}
    arows = act.get("sessions", []) if isinstance(act, dict) else []
    if arows:
        for s in arows:
            stage = s.get("funnel_stage", "")
            ts = s.get("checkout_at") or s.get("entered_at") or ""
            t  = _fmt_time(ts) if isinstance(ts, str) else ""
            if stage == "purchased":
                total = float(s.get("receipt_total") or 0)
                items_n = int(s.get("receipt_items") or 0)
                sp = s.get("receipt_salesperson") or "—"
                line = f"{t} · ✓ Purchase {_inr(total)} ({items_n} items) · salesperson {sp}"
            elif stage == "checkout_queued":
                line = f"{t} · ⏳ Reached the counter, did not transact"
            elif stage == "engaged":
                line = f"{t} · 👀 Engaged with a shelf"
            elif stage == "browsed":
                line = f"{t} · 🛍️ Browsed a shelf"
            else:
                line = f"{t} · → Entered the store"
            st.markdown(f"<div class='activity'>{line}</div>", unsafe_allow_html=True)
    else:
        st.info("No activity yet.")

with ra:
    st.subheader("Anomalies")
    anoms = _get("/anomalies", hours=hours) or {}
    awrows = anoms.get("anomalies", []) if isinstance(anoms, dict) else []
    if awrows:
        for a in awrows[:8]:
            sev = a.get("severity", "info")
            kind = a.get("kind", "")
            d = a.get("details", {}) or {}
            cls = "anomaly alert" if sev == "alert" else "anomaly"
            if kind == "footfall_outlier":
                msg = (
                    f"<b>Footfall outlier</b> — {int(d.get('footfall', 0))} entries "
                    f"vs. baseline ~{d.get('baseline_mean', 0):.0f} "
                    f"(z = {d.get('z_score', 0):.1f})"
                )
            elif kind == "dead_zone":
                z = (d.get("zone_id") or "").replace("shelf_", "").replace("_", " ").title()
                msg = (
                    f"<b>Dead zone</b> — {z}: {int(d.get('visitors_last_hour', 0))} visitors "
                    f"vs. typical {d.get('baseline_median', 0):.0f}"
                )
            elif kind == "conversion_drop":
                msg = (
                    f"<b>Conversion drop</b> — "
                    f"{d.get('drop_pct', 0):.0f}% vs. prior 3h avg"
                )
            else:
                msg = f"<b>{kind}</b> — {d}"
            st.markdown(f"<div class='{cls}'>{msg}</div>", unsafe_allow_html=True)
    else:
        st.success("No anomalies in this window.")


# --------------------------------------------------------------------------
# Footer
# --------------------------------------------------------------------------


now_s = datetime.now(UTC).strftime("%H:%M:%S UTC")
st.caption(f"Updated {now_s} · Purplle Tech Challenge 2026 · Round 2")

if auto:
    time.sleep(REFRESH_S)
    st.rerun()
