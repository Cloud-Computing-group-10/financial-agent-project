"""Streamlit frontend. It only talks to the FastAPI service over HTTP (POST /ask, GET /health).

  streamlit run frontend/app.py

The API address comes from FINQA_API_URL (default http://localhost:8080). The colours are set in
.streamlit/config.toml; the few styles below only shape the answer card and the status label.
"""
import html
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

# status -> (label, colour of the small dot in front of it)
STATUS_LABELS = {
    "answered": ("Answered", "#1F3A5F"),
    "partial": ("Partly answered", "#B7791F"),
    "abstain": ("Declined: not enough evidence or out of scope", "#B7791F"),
    "clarify": ("Needs clarification", "#64748B"),
    "error": ("Error", "#B42318"),
}

STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
html, body, [class*="css"], .stMarkdown, .stTextInput input, button { font-family: 'Inter', sans-serif; }
.block-container { max-width: 960px; padding-top: 2.5rem; }
h1 { font-weight: 600; letter-spacing: -0.02em; font-size: 1.9rem !important; }
h3 { font-weight: 600; font-size: 1.05rem !important; color: #334155; margin-top: 1.6rem; }
.status { display: inline-flex; align-items: center; gap: .45rem; font-size: .82rem; font-weight: 500;
          color: #334155; border: 1px solid #E2E5EA; border-radius: 999px; padding: .2rem .75rem; }
.status .dot { width: .5rem; height: .5rem; border-radius: 50%; }
.note { font-size: .85rem; color: #475569; background: #F5F6F8; border-radius: 8px; padding: .5rem .8rem;
        margin: .3rem 0; }
.note b { color: #1C2430; font-weight: 500; }
.meta { font-size: .8rem; color: #64748B; margin-top: .9rem; }
.connected { font-size: .82rem; color: #475569; }
</style>
"""


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


def note(kind: str, text: str) -> None:
    st.markdown(f'<div class="note"><b>{kind}</b> · {html.escape(text)}</div>', unsafe_allow_html=True)


def show_result(res: dict) -> None:
    label, color = STATUS_LABELS.get(res["status"], (res["status"], "#64748B"))
    st.markdown(f'<span class="status"><span class="dot" style="background:{color}"></span>{label}</span>',
                unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown(res["answer"])

    for text in res.get("assumptions", []):
        note("Assumption", text)
    for text in res.get("flags", []):
        note("Check", text)

    if res.get("figures"):
        st.subheader("Figures from the database")
        st.dataframe(
            [{"ref": f["ref"], "period": f["period"], "value": f["value"], "unit": f["unit"],
              "source": f["locator"], "filing": f["url"]} for f in res["figures"]],
            column_config={"filing": st.column_config.LinkColumn("filing", display_text="open on SEC")},
            hide_index=True, use_container_width=True,
        )

    if res.get("citations"):
        st.subheader("Passages from the filing")
        for c in res["citations"]:
            score = "financial statement" if c.get("score") is None else f"score {c['score']}"
            with st.expander(f"[{c['ref']}]  {c['locator']}  ·  {score}"):
                st.write(c["text"])
                st.markdown(f"[Open the filing on SEC]({c['url']})")

    usage = res.get("usage", {})
    st.markdown(
        f'<div class="meta">{res.get("latency_s", 0):.1f} s · {usage.get("input_tokens", 0):,} input / '
        f'{usage.get("output_tokens", 0):,} output tokens · {usage.get("llm_calls", 0)} LLM calls · '
        f'{html.escape(str(res.get("llm")))}</div>', unsafe_allow_html=True)

    with st.expander("Workflow trace (parse, route, SQL, search rounds)"):
        st.json(res.get("trace", {}))


def main() -> None:
    st.set_page_config(page_title="10-Q Research Agent", layout="centered")
    st.markdown(STYLE, unsafe_allow_html=True)
    st.title("10-Q Research Agent")
    st.caption("Answers questions about SEC 10-Q filings. Every number and reason is traced to its source.")

    health = get_health()
    with st.sidebar:
        st.subheader("Settings")
        if health is None:
            st.error(f"API not reachable at {API_URL}")
            presets = ["claude"]
        else:
            st.markdown('<div class="connected">● API connected</div>', unsafe_allow_html=True)
            presets = health["presets"]
        llm = st.selectbox("LLM", presets, index=presets.index("claude") if "claude" in presets else 0)
        model = st.text_input("Model id (optional)", placeholder="preset default")
        method = st.radio("Search method (for 'why' questions)", ["hybrid", "semantic", "bm25"])

        st.subheader("Examples")
        for example in EXAMPLES:
            if st.button(example, use_container_width=True):
                st.session_state.question = example

    question = st.text_input("Your question", key="question", label_visibility="collapsed",
                             placeholder="Ask about a company's 10-Q, e.g. Why did 3M's sales drop in Q2 2023?")
    if st.button("Ask", type="primary", disabled=not question or health is None):
        with st.spinner("Reading the filings..."):
            try:
                st.session_state.result = ask(question, llm, model, method)
            except requests.RequestException as e:
                st.error(f"Request failed: {e}")
                st.session_state.result = None

    if st.session_state.get("result"):
        show_result(st.session_state.result)


main()
