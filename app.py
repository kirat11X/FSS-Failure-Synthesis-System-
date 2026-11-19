"""Streamlit front end for the FSS demo: pick a scenario, ask your own question,
see the full report with one tab per part. Live answers come from a small local model
(default qwen2.5:3b) served by Ollama; the stub replays each scenario's stored answer.

    streamlit run app.py --server.address 127.0.0.1 --server.port 8501

Scenarios run in-process (scenarios.run_scenario with the typed question). Y02 and G01
are special demo runs, so they go through demo/run_demo.py.
"""
import dataclasses
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from fss_py.llm import LLMError, OllamaLLM, StubLLM  # noqa: E402
from fss_py.presentation import render_investigation  # noqa: E402
from scenarios import SCENARIOS, run_scenario, stub_answer_json  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")
SECTION = re.compile(r"^\[([1-8])\] ")
SPECIAL = {"Y02": "Hallucinated citation caught by the validator",
           "G01": "Domain swap: five packs, same code"}
TABS = ["1 Question", "2 Retrieval", "3 Events", "4 Data gaps",
        "5 Knowledge", "6 LLM context", "7 Synthesis", "8 Validator"]


@st.cache_data(ttl=30, show_spinner=False)
def ollama_models() -> tuple[list[str], str | None]:
    """Models pulled into the local Ollama server, or the reason none can be used."""
    llm = OllamaLLM()
    reason = llm.available()
    if reason and "is not pulled" not in reason:
        return [], reason
    try:
        with urllib.request.urlopen(f"{llm.url}/api/tags", timeout=3) as r:
            names = sorted(m["name"] for m in json.load(r).get("models", []))
    except (OSError, ValueError) as exc:
        return [], str(exc)
    return names, None if names else "no model pulled (run: ollama pull qwen2.5:3b)"


@st.cache_data(show_spinner=False)
def run_question(sid: str, question: str, model: str | None, short: bool) -> str:
    """Run one scenario with the given question; return the plain-text report.
    model=None uses the stub LLM, otherwise that Ollama model."""
    sc = SCENARIOS[sid]
    llm = OllamaLLM(model) if model else StubLLM(stub_answer_json(sc))
    inv = run_scenario(dataclasses.replace(sc, query=question), llm,
                       max_context_requests=0 if model else 1)
    who = f"live, {model} (local, Ollama)" if model else "stub (canned answer stored with the scenario)"
    line = "=" * 80
    head = f"{line}\n  {sid} | {sc.pack} | {sc.title}\n{line}\nProves: {sc.proves}\nLLM: {who}\n"
    return head + render_investigation(inv, source_names=sc.source_names, colour=False,
                                       show_context=not short)


@st.cache_data(show_spinner=False)
def run_special(sid: str) -> tuple[str, str, int]:
    p = subprocess.run([sys.executable, "demo/run_demo.py", sid, "--no-colour"], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    return ANSI.sub("", p.stdout), ANSI.sub("", p.stderr), p.returncode


def split_reports(out: str) -> list[tuple[str, dict[int, str]]]:
    """Split output into (header, {part number: text}) per scenario report."""
    reports: list[tuple[str, dict[int, list[str]]]] = []
    header: list[str] = []
    current: dict[int, list[str]] | None = None
    part = 0
    for line in out.splitlines():
        m = SECTION.match(line)
        if m:
            n = int(m.group(1))
            if n == 1 or current is None:
                current = {}
                reports.append(("\n".join(header).strip(), current))
                header = []
            part = n
            current[part] = [line]
        elif current is not None and part == 8 and line.startswith("="):
            current, part = None, 0  # banner of the next scenario
            header = [line]
        elif current is not None:
            current[part].append(line)
        else:
            header.append(line)
    return [(h, {n: "\n".join(ls).rstrip() for n, ls in parts.items()}) for h, parts in reports]


def show_verdict(text: str) -> None:
    if "REJECTED" in text:
        st.error("Validator verdict: REJECTED")
    elif "FAILED" in text:
        st.error("Validator verdict: FAILED")
    elif "CORRECTIONS" in text:
        st.warning("Validator verdict: ACCEPTED WITH CORRECTIONS")
    elif "ACCEPTED" in text:
        st.success("Validator verdict: ACCEPTED")


def show_report(out: str) -> None:
    reports = split_reports(out)
    if not reports:
        st.code(out, language=None)
    for header, parts in reports:
        st.code(header, language=None)
        show_verdict(parts.get(8, ""))
        for tab, n in zip(st.tabs(TABS), range(1, 9)):
            with tab:
                st.code(parts.get(n, "(part not in output)"), language=None)
    with st.expander("Raw output"):
        st.code(out, language=None)


st.set_page_config(page_title="FSS demo", layout="wide")
st.title("Failure Synthesis System: demo")
st.caption("Recorded binary events -> bounded retrieval -> deterministic decoder -> "
           "evidence -> domain docs -> LLM -> output validator")

labels = {f"{sid} - {sc.title}": sid for sid, sc in SCENARIOS.items()}
labels.update({f"{sid} - {title}": sid for sid, title in SPECIAL.items()})
sid = labels[st.sidebar.selectbox("Scenario", list(labels))]
short = st.sidebar.checkbox("Hide full LLM context", value=False)

if sid in SPECIAL:
    st.sidebar.caption("Fixed demo run: no question to type.")
    if st.sidebar.button("Run", type="primary"):
        out, err, rc = run_special(sid)
        if rc != 0:
            st.error(f"run_demo.py exited with {rc}")
        if err.strip():
            st.code(err)
        show_report(out)
    else:
        st.info("Press Run in the sidebar.")
else:
    sc = SCENARIOS[sid]
    models, unavailable = ollama_models()
    mode = st.sidebar.radio("LLM", ["Live LLM (local Qwen)", "Stub (offline, canned answer)"])
    live = mode.startswith("Live")
    model = None
    if live:
        if unavailable:
            st.sidebar.error(f"Live LLM not available: {unavailable}")
        else:
            default = models.index("qwen2.5:3b") if "qwen2.5:3b" in models else 0
            model = st.sidebar.selectbox("Model", models, index=default,
                                         help="Runs on this server's CPU. No API key needed.")

    with st.form("ask"):
        question = st.text_area("Your question", value=sc.query, height=80)
        st.caption(f"Data: {sc.pack}, the scenario's fixed time window. "
                   f"Example question: \u201c{sc.query}\u201d")
        submitted = st.form_submit_button("Investigate", type="primary",
                                          disabled=live and model is None)
    if submitted:
        question = question.strip()
        if not question:
            st.error("Type a question first.")
        else:
            if not live and question != sc.query:
                st.warning("The stub LLM replays the answer stored for the example question, so "
                           "part 7 will not answer your question. Choose Live LLM for a real "
                           "answer.")
            try:
                with st.spinner(f"Investigating with {model}... (about 30-90 s on CPU)"
                                if live else "Investigating..."):
                    out = run_question(sid, question, model, short)
            except LLMError as exc:
                st.error(f"LLM call failed: {exc}")
            else:
                show_report(out)
    else:
        st.info("Type a question (or keep the example) and press Investigate.")
