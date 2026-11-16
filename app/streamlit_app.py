"""Minimal chat UI. Run:  streamlit run app/streamlit_app.py   (pip install streamlit)

Mirrors the production flow: the UI collects the question + authenticated role; everything else
(scope check, allowlisted tools, RBAC, validation, fail-closed) happens in ops_assistant.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ops_assistant.answer import answer_question  # noqa: E402
from ops_assistant.db import build_database  # noqa: E402
from ops_assistant.suggestions import DEFAULT_QUESTIONS  # noqa: E402

st.set_page_config(page_title="Operations Assistant (prototype)")
st.title("AI Operations Assistant")
st.caption("Prototype on synthetic data. Answers are retrieved from approved results and verified before display.")


@st.cache_resource
def get_conn():
    return build_database()


role = st.sidebar.selectbox("Signed in as", ["production_manager", "supply_manager"])
stale = st.sidebar.checkbox("Simulate stale upstream data (+3 days)")
# Fixed demo clock so the synthetic 2026-11-15 data is 'fresh'; the checkbox pushes it past the 24h SLA.
now = datetime(2026, 11, 15, 9, 0) + (timedelta(days=3) if stale else timedelta())

pending = None
st.write("**Common questions**")
cols = st.columns(2)
for i, dq in enumerate(DEFAULT_QUESTIONS[role]):
    if cols[i % 2].button(dq, key=f"dq{i}", use_container_width=True):
        pending = dq
typed = st.chat_input("Ask about production, inventory, safety stock or material reorders")

if q := (pending or typed):
    st.chat_message("user").write(q)
    r = answer_question(q, role, get_conn(), now=now)
    with st.chat_message("assistant"):
        st.markdown(r.text)
        st.caption(f"response class: {r.status}")
        with st.expander("Evidence & alerts"):
            st.json([e.__dict__ for e in r.evidence])
            st.json(r.alerts.events)
