"""Streamlit dashboard.

Reads from the API service (not directly from the DB) so the dashboard
keeps working against any deployment that exposes the documented HTTP
contract. Auto-refreshes every 5 s.

Layout:
  • Header KPIs: footfall, unique visitors, conversion %, avg dwell
  • Funnel waterfall (entered → purchased)
  • Zone heat: unique visitors per shelf
  • Anomalies feed
  • Camera health + recent events tail
  • Session lookup
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any

import httpx
import pandas as pd
import streamlit as st

API_BASE = os.environ.get("API_BASE", "http://api:8000")
REFRESH_S = int(os.environ.get("DASHBOARD_REFRESH_S", "5"))

st.set_page_config(
    page_title="Store Intelligence — Brigade Road",
    page_icon=":bar_chart:",
    layout="wide",
)


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------


def _get(path: str, **params: Any) -> Mapping[str, Any] | list[Any] | None:
    try:
        r = httpx.get(f"{API_BASE}{path}", params=params, timeout=4.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.warning(f"API call failed: `GET {path}` — {e}")
        return None


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------


with st.sidebar:
    st.markdown("### Store ST1008 — Brigade Road")
    hours = st.slider("Window (hours)", 1, 168, 24, step=1)
    auto = st.toggle("Auto-refresh", value=True)
    st.caption(f"API: `{API_BASE}`")
    health = _get("/readyz")
    if isinstance(health, dict):
        ok = health.get("status") == "ready"
        st.success("System ready") if ok else st.warning(f"Degraded: {health}")
    st.caption(f"Refresh every {REFRESH_S}s")


# --------------------------------------------------------------------------
# Header KPIs
# --------------------------------------------------------------------------


st.title("Store Intelligence")
metrics = _get("/metrics", hours=hours) or {}

c1, c2, c3, c4 = st.columns(4)
c1.metric("Footfall",         f"{int(metrics.get('footfall', 0)):,}")
c2.metric("Unique visitors",  f"{int(metrics.get('unique_visitors', 0)):,}")
conv = float(metrics.get("conversion_rate", 0.0)) * 100
c3.metric("Conversion",       f"{conv:.1f}%")
avg = float(metrics.get("avg_session_duration_s", 0.0))
c4.metric("Avg dwell",        f"{avg:.0f}s")


# --------------------------------------------------------------------------
# Funnel waterfall
# --------------------------------------------------------------------------


st.subheader("Funnel")
funnel = _get("/funnel", hours=hours) or {}
stages = funnel.get("stages", {}) if isinstance(funnel, dict) else {}
ORDER = ["entered", "browsed", "engaged", "checkout_queued", "purchased"]
df_funnel = pd.DataFrame(
    {"stage": ORDER, "sessions": [int(stages.get(s, 0)) for s in ORDER]}
)
st.bar_chart(df_funnel.set_index("stage"), height=240)


# --------------------------------------------------------------------------
# Zones
# --------------------------------------------------------------------------


left, right = st.columns([3, 2])

with left:
    st.subheader("Zone engagement")
    zones = _get("/zones", hours=hours) or {}
    rows = zones.get("zones", []) if isinstance(zones, dict) else []
    if rows:
        df_z = pd.DataFrame(rows)
        df_z = df_z.sort_values("unique_visitors", ascending=False)
        st.dataframe(
            df_z,
            hide_index=True,
            use_container_width=True,
            column_config={
                "zone_id":        st.column_config.TextColumn("Zone"),
                "unique_visitors":st.column_config.NumberColumn("Visitors"),
                "total_dwell_s":  st.column_config.NumberColumn("Total dwell (s)", format="%.0f"),
                "avg_dwell_s":    st.column_config.NumberColumn("Avg dwell (s)",   format="%.1f"),
            },
        )
    else:
        st.info("No zone visits yet.")

with right:
    st.subheader("Cameras")
    cams = _get("/cameras") or {}
    crows = cams.get("cameras", []) if isinstance(cams, dict) else []
    if crows:
        df_c = pd.DataFrame(crows)
        st.dataframe(df_c, hide_index=True, use_container_width=True)
    else:
        st.info("No camera activity in the last 5 minutes.")


# --------------------------------------------------------------------------
# Anomalies
# --------------------------------------------------------------------------


st.subheader(f"Anomalies (last {hours}h)")
anoms = _get("/anomalies", hours=hours) or {}
arows = anoms.get("anomalies", []) if isinstance(anoms, dict) else []
if arows:
    df_a = pd.DataFrame(arows)
    if "detected_at" in df_a:
        df_a["detected_at"] = pd.to_datetime(df_a["detected_at"])
    st.dataframe(
        df_a[["detected_at", "kind", "severity", "details"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "detected_at": st.column_config.DatetimeColumn("Detected"),
            "kind":        st.column_config.TextColumn("Kind"),
            "severity":    st.column_config.TextColumn("Severity"),
            "details":     st.column_config.JsonColumn("Details", width="large"),
        },
    )
else:
    st.success("No anomalies in this window.")


# --------------------------------------------------------------------------
# Recent events + session lookup
# --------------------------------------------------------------------------


with st.expander("Recent events (stream tail)"):
    recent = _get("/events/recent", n=50) or {}
    erows = recent.get("events", []) if isinstance(recent, dict) else []
    if erows:
        df_e = pd.DataFrame(erows)[
            ["ts", "type", "camera_id", "role", "embedding_id", "payload"]
        ]
        st.dataframe(df_e, hide_index=True, use_container_width=True, height=280)


with st.expander("Session lookup"):
    sid = st.text_input("Session ID (UUID)", value="")
    if sid:
        s = _get(f"/sessions/{sid}")
        if s:
            st.json(s)


# --------------------------------------------------------------------------
# Auto-refresh
# --------------------------------------------------------------------------


if auto:
    time.sleep(REFRESH_S)
    st.rerun()
