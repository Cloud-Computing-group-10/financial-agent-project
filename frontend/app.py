"""Streamlit frontend. It only talks to the FastAPI service over HTTP (POST /ask, GET /health).

  streamlit run frontend/app.py

The API address comes from FINQA_API_URL (default http://localhost:8080).
"""
import os

import requests
import streamlit as st

API_URL = os.environ.get("FINQA_API_URL", "http://localhost:8080").rstrip("/")

EXAMPLES = [
    "What was 3M's revenue in Q2 2023?",
    "How much did Caterpillar's operating profit change year over year in Q2 2023?",
    "Why did 3M's sales drop in Q2 2023?",
    "What was Coca-Cola's net income in Q4 2023?",
]

STATUS_LABELS = {
    "answered": ("Answered", "green"),
    "partial": ("Partly answered", "orange"),
    "abstain": ("Declined: not enough evidence or out of scope", "orange"),
    "clarify": ("Needs clarification", "blue"),
    "error": ("Error", "red"),
}


def get_health() -> dict | None:
    try:
        return requests.get(f"{API_URL}/health", timeout=5).json()
    except requests.RequestException:
        return None


def ask(question: str, llm: str, model: str, method: str) -> dict:
    body = {"question": question, "method": method, "llm": llm, "model": model or None}
    r = requests.post(f"{API_URL}/ask", json=body, timeout=300)
    r.raise_for_status()
    return r.json()


def show_result(res: dict) -> None:
    label, color = STATUS_LABELS.get(res["status"], (res["status"], "gray"))
    st.markdown(f":{color}-badge[{label}]")
    st.markdown(res["answer"])

    for note in res.get("assumptions", []):
        st.info(note)
    for flag in res.get("flags", []):
        st.warning(flag)

    if res.get("figures"):
        st.subheader("Figures (from the database)")
        st.dataframe(
            [{"ref": f["ref"], "period": f["period"], "value": f["value"], "unit": f["unit"],
              "source": f["locator"], "filing": f["url"]} for f in res["figures"]],
            column_config={"filing": st.column_config.LinkColumn("filing", display_text="open on SEC")},
            hide_index=True,
        )

    if res.get("citations"):
        st.subheader("Passages (from the filing text)")
        for c in res["citations"]:
            with st.expander(f"[{c['ref']}] {c['locator']}  (score {c['score']})"):
                st.write(c["text"])
                st.markdown(f"[Open the filing on SEC]({c['url']})")

    usage = res.get("usage", {})
    cols = st.columns(4)
    cols[0].metric("Latency", f"{res.get('latency_s', 0):.1f} s")
    cols[1].metric("Input tokens", usage.get("input_tokens", 0))
    cols[2].metric("Output tokens", usage.get("output_tokens", 0))
    cols[3].metric("LLM calls", usage.get("llm_calls", 0))
    st.caption(f"Model: {res.get('llm')}")

    with st.expander("Workflow trace (parse, route, SQL, search rounds)"):
        st.json(res.get("trace", {}))


def main() -> None:
    st.set_page_config(page_title="10-Q Research Agent", layout="wide")
    st.title("10-Q Research Agent")
    st.caption("Answers questions about SEC 10-Q filings. Every number and reason is traced to its source.")

    health = get_health()
    with st.sidebar:
        st.header("Settings")
        if health is None:
            st.error(f"API not reachable at {API_URL}")
            presets = ["claude"]
        else:
            st.success("API connected")
            presets = health["presets"]
        llm = st.selectbox("LLM", presets, index=presets.index("claude") if "claude" in presets else 0)
        model = st.text_input("Model id (optional)", placeholder="preset default")
        method = st.radio("Search method (for 'why' questions)", ["semantic", "bm25", "hybrid"])

        st.header("Examples")
        for example in EXAMPLES:
            if st.button(example, use_container_width=True):
                st.session_state.question = example

    question = st.text_input("Your question", key="question",
                             placeholder="e.g. Why did 3M's sales drop in Q2 2023?")
    if st.button("Ask", type="primary", disabled=not question or health is None):
        with st.spinner("Thinking..."):
            try:
                st.session_state.result = ask(question, llm, model, method)
            except requests.RequestException as e:
                st.error(f"Request failed: {e}")
                st.session_state.result = None

    if st.session_state.get("result"):
        show_result(st.session_state.result)


main()
