from __future__ import annotations

import io
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd
import streamlit as st

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None

try:
    from docx import Document
except ImportError:  # pragma: no cover
    Document = None

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


APP_TITLE = "Handover Intelligence"
CATEGORIES = ["Decision", "Question", "Risk", "Responsibility", "Dependency", "Next action"]
STATUS_OPTIONS = ["Draft", "Approved", "Needs evidence", "Conflicting", "Rejected"]


@dataclass
class Evidence:
    evidence_id: str
    source_name: str
    source_type: str
    text: str
    locator: str = ""
    source_url: str = ""
    author: str = ""
    timestamp: str = ""
    sensitivity: str = "Project"


@dataclass
class Claim:
    claim_id: str
    category: str
    text: str
    evidence_ids: list[str] = field(default_factory=list)
    confidence: str = "Medium"
    status: str = "Draft"
    reviewer_note: str = ""
    owner: str = ""


SAMPLE_EVIDENCE = [
    Evidence("E001", "Kickoff notes", "Meeting note", "The team agreed to ship the pilot with manual approval before adding automated publishing.", "p. 1", "https://example.com/kickoff", "Maya", "2026-10-04"),
    Evidence("E002", "Engineering notes", "Repository note", "Decision: use PostgreSQL and pgvector for the evidence store. We should revisit this after the first 10,000 records.", "README.md", "https://github.com/example/project/blob/main/README.md", "Arun", "2026-10-10"),
    Evidence("E003", "Weekly sync", "Meeting note", "The Drive connector is blocked by missing OAuth scopes. This is the main launch risk and may delay the demo.", "p. 2", "https://example.com/weekly-sync", "Jiya", "2026-10-18"),
    Evidence("E004", "Task board export", "Task board", "Next action: Arun owns the connector retry flow. Jiya will prepare the reviewer walkthrough by Friday.", "TASK-42", "https://linear.app/example/issue/TASK-42", "Project bot", "2026-10-20"),
    Evidence("E005", "Project chat", "Chat", "Open question: should restricted HR notes be excluded at ingestion or only redacted at publication? Security needs to confirm.", "#handover", "https://chat.example.com/handover/123", "Sam", "2026-10-21", "Restricted"),
    Evidence("E006", "Project chat", "Chat", "The launch depends on the identity provider configuration being approved by the platform team.", "#platform", "https://chat.example.com/platform/456", "Sam", "2026-10-22"),
]


def init_state() -> None:
    defaults = {
        "evidence": [],
        "claims": [],
        "analysis_ran": False,
        "project_name": "Project Handover",
        "recipient_role": "Incoming project member",
        "allowed_sensitivity": ["Project"],
        "reviewer": "Jiya Joyardar",
        "llm_used": False,
        "analysis_notice": "",
        "use_llm": False,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def split_sentences(text: str) -> list[str]:
    text = clean_text(text)
    if not text:
        return []
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [chunk.strip(" -•\t") for chunk in chunks if len(chunk.strip()) >= 18]


def detect_type(filename: str) -> str:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else "txt"
    return {"md": "Markdown", "txt": "Text", "csv": "CSV", "json": "JSON", "pdf": "PDF", "docx": "DOCX"}.get(suffix, suffix.upper())


def read_upload(uploaded_file) -> list[Evidence]:
    raw = uploaded_file.getvalue()
    filename = uploaded_file.name
    source_type = detect_type(filename)
    text = ""
    metadata_rows: list[dict[str, Any]] = []

    if filename.lower().endswith(".pdf"):
        if PdfReader is None:
            st.warning("PDF support is unavailable; install pypdf.")
            return []
        reader = PdfReader(io.BytesIO(raw))
        for page_no, page in enumerate(reader.pages, start=1):
            page_text = clean_text(page.extract_text())
            if page_text:
                metadata_rows.append({"text": page_text, "locator": f"page {page_no}"})
    elif filename.lower().endswith(".docx"):
        if Document is None:
            st.warning("DOCX support is unavailable; install python-docx.")
            return []
        document = Document(io.BytesIO(raw))
        text = "\n".join(p.text for p in document.paragraphs)
    elif filename.lower().endswith(".csv"):
        df = pd.read_csv(io.BytesIO(raw))
        text = "\n".join(" | ".join(f"{column}: {row[column]}" for column in df.columns) for _, row in df.iterrows())
    elif filename.lower().endswith(".json"):
        parsed = json.loads(raw.decode("utf-8", errors="ignore"))
        text = json.dumps(parsed, indent=2, ensure_ascii=False)
    else:
        text = raw.decode("utf-8", errors="ignore")

    if text:
        metadata_rows = [{"text": text, "locator": "uploaded file"}]

    evidence: list[Evidence] = []
    for index, row in enumerate(metadata_rows, start=1):
        evidence.append(Evidence(
            evidence_id=f"UP{len(st.session_state.evidence) + index:03d}",
            source_name=filename,
            source_type=source_type,
            text=clean_text(row["text"]),
            locator=row.get("locator", ""),
        ))
    return evidence


def visible_evidence() -> list[Evidence]:
    allowed = set(st.session_state.allowed_sensitivity)
    return [item for item in st.session_state.evidence if item.sensitivity in allowed]


def add_claim(claims: list[Claim], category: str, text: str, evidence: Evidence, confidence: str = "Medium") -> None:
    text = clean_text(text)
    if len(text) < 18:
        return
    key = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if any(re.sub(r"[^a-z0-9]+", " ", c.text.lower()).strip() == key for c in claims):
        return
    claims.append(Claim(
        claim_id=f"C{len(claims) + 1:03d}",
        category=category,
        text=text,
        evidence_ids=[evidence.evidence_id],
        confidence=confidence,
    ))


def heuristic_extract(evidence_items: Iterable[Evidence]) -> list[Claim]:
    claims: list[Claim] = []
    for evidence in evidence_items:
        for sentence in split_sentences(evidence.text):
            lower = sentence.lower()
            if "?" in sentence or any(token in lower for token in ["open question", "unknown", "need to confirm", "needs to confirm"]):
                add_claim(claims, "Question", sentence, evidence, "Medium")
            if any(token in lower for token in ["risk", "blocked", "blocker", "may delay", "at risk", "critical"]):
                add_claim(claims, "Risk", sentence, evidence, "High" if any(token in lower for token in ["blocked", "critical"]) else "Medium")
            if any(token in lower for token in ["decision:", "decided", "agreed", "approved", "chosen", "use "]):
                add_claim(claims, "Decision", sentence, evidence, "High" if "decision" in lower or "agreed" in lower else "Medium")
            if any(token in lower for token in ["owns", "owner", "responsible", "will prepare", "will lead"]):
                owner_match = re.search(r"([A-Z][a-z]+)\s+(?:owns|will|is responsible)", sentence)
                claim = sentence if not owner_match else f"{owner_match.group(1)}: {sentence}"
                add_claim(claims, "Responsibility", claim, evidence, "Medium")
            if any(token in lower for token in ["depends on", "dependency", "requires", "blocked by"]):
                add_claim(claims, "Dependency", sentence, evidence, "Medium")
            if any(token in lower for token in ["next action", "todo", "to-do", "should ", "action:", "will prepare"]):
                add_claim(claims, "Next action", sentence, evidence, "Medium")
    return claims


def llm_extract(evidence_items: list[Evidence]) -> list[Claim]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return []
    client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_API_BASE")) if os.getenv("OPENAI_API_BASE") else OpenAI(api_key=api_key)
    evidence_text = "\n".join(f"[{e.evidence_id}] {e.text}" for e in evidence_items)
    prompt = f"""Extract a project handover from the evidence below. Return JSON only as a list of objects with keys category, text, evidence_ids, confidence. Categories: {', '.join(CATEGORIES)}. Never invent facts. Use confidence High, Medium, or Low. Every item must cite one or more evidence IDs.\n\n{evidence_text}"""
    try:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a careful evidence-grounded project handover analyst."},
                {"role": "user", "content": prompt},
            ],
        )
        payload = json.loads(response.choices[0].message.content)
        rows = payload if isinstance(payload, list) else payload.get("claims", [])
        claims = []
        for index, row in enumerate(rows, start=1):
            if row.get("category") in CATEGORIES and row.get("text") and row.get("evidence_ids"):
                claims.append(Claim(
                    claim_id=f"C{index:03d}", category=row["category"], text=clean_text(row["text"]),
                    evidence_ids=list(row["evidence_ids"]), confidence=row.get("confidence", "Medium"),
                ))
        return claims
    except Exception as exc:  # Keep the MVP usable if an optional provider fails.
        st.warning(f"Optional AI extraction failed; using deterministic extraction instead. Details: {exc}")
        return []


def run_analysis() -> None:
    filtered = visible_evidence()
    if not filtered:
        st.session_state.claims = []
        st.session_state.analysis_ran = True
        st.session_state.analysis_notice = "No evidence is visible under the current access policy."
        return
    llm_claims = llm_extract(filtered) if st.session_state.get("use_llm", False) else []
    st.session_state.claims = llm_claims or heuristic_extract(filtered)
    st.session_state.analysis_ran = True
    st.session_state.llm_used = bool(llm_claims)
    st.session_state.analysis_notice = f"Analysis complete — {len(st.session_state.claims)} traceable claims are ready for review."


def evidence_lookup() -> dict[str, Evidence]:
    return {item.evidence_id: item for item in st.session_state.evidence}


def status_counts() -> dict[str, int]:
    return {status: sum(1 for claim in st.session_state.claims if claim.status == status) for status in STATUS_OPTIONS}


def markdown_export() -> str:
    lookup = evidence_lookup()
    lines = [f"# {st.session_state.project_name} — Handover", "", f"**Recipient:** {st.session_state.recipient_role}", f"**Reviewer:** {st.session_state.reviewer}", f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", "", "## Review summary", "", f"Approved items: {sum(c.status == 'Approved' for c in st.session_state.claims)} / {len(st.session_state.claims)}", ""]
    for category in CATEGORIES:
        rows = [claim for claim in st.session_state.claims if claim.category == category and claim.status != "Rejected"]
        if not rows:
            continue
        lines += [f"## {category}", ""]
        for claim in rows:
            evidence_links = []
            for evidence_id in claim.evidence_ids:
                evidence = lookup.get(evidence_id)
                if evidence:
                    label = f"{evidence.source_name} ({evidence.locator})".strip()
                    evidence_links.append(f"[{label}]({evidence.source_url})" if evidence.source_url else label)
            lines.append(f"- **{claim.status} · {claim.confidence} confidence:** {claim.text} _(Evidence: {'; '.join(evidence_links) or 'missing'})_")
            if claim.reviewer_note:
                lines.append(f"  - Reviewer note: {claim.reviewer_note}")
        lines.append("")
    lines += ["## Evidence appendix", ""]
    for evidence in visible_evidence():
        lines.append(f"- **{evidence.evidence_id} — {evidence.source_name}**: {evidence.text}")
    return "\n".join(lines)


def render_sidebar() -> None:
    with st.sidebar:
        st.header("Project setup")
        st.session_state.project_name = st.text_input("Project name", st.session_state.project_name)
        st.session_state.recipient_role = st.text_input("Recipient role", st.session_state.recipient_role)
        st.session_state.reviewer = st.text_input("Reviewer", st.session_state.reviewer)
        st.divider()
        st.subheader("Evidence intake")
        uploaded = st.file_uploader("Upload notes, exports or documents", type=["txt", "md", "csv", "json", "pdf", "docx"], accept_multiple_files=True)
        if uploaded and st.button("Add uploaded evidence", use_container_width=True):
            additions = [item for file in uploaded for item in read_upload(file)]
            st.session_state.evidence.extend(additions)
            st.session_state.analysis_ran = False
            st.success(f"Added {len(additions)} evidence item(s).")
        if st.button("Load sample project", use_container_width=True):
            st.session_state.evidence = [Evidence(**asdict(item)) for item in SAMPLE_EVIDENCE]
            st.session_state.claims = []
            st.session_state.analysis_ran = False
        st.divider()
        st.subheader("Access policy")
        st.caption("Only evidence in the selected sensitivity groups is analysed and exported.")
        st.session_state.allowed_sensitivity = st.multiselect("Allowed sensitivity", ["Project", "Restricted"], default=st.session_state.allowed_sensitivity)
        st.session_state.use_llm = st.toggle("Use optional OpenAI extraction", value=False, help="Requires OPENAI_API_KEY in the deployment secrets. Deterministic extraction works without it.")
        if st.button("Analyse project →", type="primary", use_container_width=True):
            run_analysis()
            st.success(st.session_state.analysis_notice)


def render_overview() -> None:
    st.markdown("<div class='section-kicker'>PROJECT PULSE</div><h2 class='section-title'>Project snapshot</h2>", unsafe_allow_html=True)
    if not st.session_state.evidence:
        st.markdown("<div class='empty-state'><div class='empty-icon'>✦</div><h3>Your handover starts here</h3><p>Add notes, task exports, meeting minutes, or load the sample project from the left rail.</p></div>", unsafe_allow_html=True)
        return
    if st.session_state.analysis_notice:
        st.success(st.session_state.analysis_notice)
    action_cols = st.columns([1.2, 3.8])
    with action_cols[0]:
        if st.button("Analyse sources →", type="primary", use_container_width=True, key="main_analyse"):
            run_analysis()
            st.success(st.session_state.analysis_notice)
    with action_cols[1]:
        if st.session_state.analysis_ran:
            st.caption("Analysis is complete. Open the Review tab to approve, edit, or flag each generated claim.")
        else:
            st.caption("Evidence is loaded. Start the extraction when you are ready.")
    counts = status_counts()
    cols = st.columns(5)
    cols[0].metric("Evidence items", len(visible_evidence()))
    cols[1].metric("Claims", len(st.session_state.claims))
    cols[2].metric("Approved", counts["Approved"])
    cols[3].metric("Needs evidence", counts["Needs evidence"])
    cols[4].metric("Conflicting", counts["Conflicting"])
    if not st.session_state.analysis_ran:
        st.warning("Evidence is loaded but has not been analysed yet.")
        return
    st.markdown("<div class='section-kicker'>SIGNAL TO ACTION</div><h3 class='section-title small'>Handover readiness</h3>", unsafe_allow_html=True)
    reviewed = sum(claim.status in {"Approved", "Rejected"} for claim in st.session_state.claims)
    readiness = round(100 * reviewed / len(st.session_state.claims)) if st.session_state.claims else 0
    st.progress(readiness / 100, text=f"{readiness}% of generated claims reviewed")
    st.caption("A claim is not ready to share until a reviewer approves it or explicitly rejects it.")
    for category in CATEGORIES:
        category_claims = [claim for claim in st.session_state.claims if claim.category == category]
        if category_claims:
            st.markdown(f"**{category}:** {len(category_claims)} item(s)")


def render_evidence() -> None:
    st.markdown("<div class='section-kicker'>SOURCE ROOM</div><h2 class='section-title'>Evidence vault</h2><p class='muted'>Every claim starts with a source. Search the trail, inspect the context, and follow the link back.</p>", unsafe_allow_html=True)
    if not st.session_state.evidence:
        st.info("No evidence loaded.")
        return
    search = st.text_input("Search evidence", placeholder="Try: launch risk, Arun, OAuth, decision…", key="evidence_search")
    evidence_items = visible_evidence()
    if search:
        evidence_items = [evidence for evidence in evidence_items if search.lower() in evidence.text.lower() or search.lower() in evidence.source_name.lower() or search.lower() in evidence.evidence_id.lower()]
        st.caption(f"Showing {len(evidence_items)} matching source(s) out of {len(visible_evidence())} visible source(s).")
        if not evidence_items:
            st.markdown(f"<div class='empty-state compact'><div class='empty-icon'>⌕</div><h3>No matching evidence</h3><p>Try a broader keyword, source name, or evidence ID such as <b>E001</b>.</p></div>", unsafe_allow_html=True)
            return
    else:
        st.caption(f"Showing all {len(evidence_items)} visible source(s).")
    for evidence in evidence_items:
        with st.expander(f"{evidence.evidence_id} · {evidence.source_name} · {evidence.source_type}"):
            st.write(evidence.text)
            cols = st.columns(3)
            cols[0].caption(f"Locator: {evidence.locator or '—'}")
            cols[1].caption(f"Author: {evidence.author or '—'}")
            cols[2].caption(f"Sensitivity: {evidence.sensitivity}")
            if evidence.source_url:
                st.link_button("Open source", evidence.source_url)


def render_review() -> None:
    st.markdown("<div class='section-kicker'>HUMAN IN THE LOOP</div><h2 class='section-title'>Review queue</h2><p class='muted'>Shape the handover before it leaves the room. Approve only what you can defend.</p>", unsafe_allow_html=True)
    if not st.session_state.claims:
        st.markdown("<div class='empty-state compact'><div class='empty-icon'>◎</div><h3>Nothing to review yet</h3><p>Load evidence, then click <b>Analyse project →</b> in the left rail. Your claims will appear here with their source IDs.</p></div>", unsafe_allow_html=True)
        return
    lookup = evidence_lookup()
    for index, claim in enumerate(st.session_state.claims):
        with st.container(border=True):
            top = st.columns([1.4, 1.2, 1.2, 1.5])
            top[0].markdown(f"**{claim.claim_id} · {claim.category}**")
            top[1].markdown(f"Confidence: **{claim.confidence}**")
            status = top[2].selectbox("Status", STATUS_OPTIONS, index=STATUS_OPTIONS.index(claim.status), key=f"status_{claim.claim_id}", label_visibility="collapsed")
            top[3].markdown(" · ".join(claim.evidence_ids) if claim.evidence_ids else "Missing evidence")
            edited = st.text_area("Claim", claim.text, key=f"text_{claim.claim_id}", height=70)
            note = st.text_input("Reviewer note", claim.reviewer_note, key=f"note_{claim.claim_id}")
            claim.text, claim.status, claim.reviewer_note = edited, status, note
            sources = [lookup[eid] for eid in claim.evidence_ids if eid in lookup]
            if sources:
                st.caption("Supporting evidence: " + " | ".join(f"{item.evidence_id}: {item.source_name} — {item.text[:160]}" for item in sources))
            else:
                st.error("This claim has no accessible supporting evidence.")


def render_handover() -> None:
    st.markdown("<div class='section-kicker'>READY TO PASS ON</div><h2 class='section-title'>Approved handover</h2><p class='muted'>A calm, audience-safe view for the person taking the next step.</p>", unsafe_allow_html=True)
    approved = [claim for claim in st.session_state.claims if claim.status == "Approved"]
    if not approved:
        st.warning("Approve at least one claim in the Review tab before sharing a handover.")
        return
    st.markdown(f"## {st.session_state.project_name}")
    st.caption(f"Prepared for: {st.session_state.recipient_role} · Reviewed by: {st.session_state.reviewer}")
    for category in CATEGORIES:
        rows = [claim for claim in approved if claim.category == category]
        if rows:
            st.markdown(f"### {category}")
            for claim in rows:
                st.markdown(f"- {claim.text}  \n  *Confidence: {claim.confidence} · Evidence: {', '.join(claim.evidence_ids)}*")
    st.download_button("Download approved handover (.md)", markdown_export(), file_name="approved-handover.md", mime="text/markdown", type="primary")
    st.download_button("Download audit data (.json)", json.dumps({"claims": [asdict(item) for item in approved], "evidence": [asdict(item) for item in visible_evidence()]}, indent=2), file_name="handover-audit.json", mime="application/json")


def render_access() -> None:
    st.markdown("<div class='section-kicker'>BOUNDARIES MATTER</div><h2 class='section-title'>Access and traceability</h2><p class='muted'>The handover is useful only when it is safe to share.</p>", unsafe_allow_html=True)
    st.markdown("The MVP applies an audience filter before analysis and again before export. In a production connector, this same layer should re-check source permissions at retrieval and publication time.")
    cols = st.columns(3)
    cols[0].metric("Allowed groups", len(st.session_state.allowed_sensitivity))
    cols[1].metric("Visible evidence", len(visible_evidence()))
    cols[2].metric("Restricted hidden", len(st.session_state.evidence) - len(visible_evidence()))
    st.markdown("### Current policy")
    st.json({"recipient_role": st.session_state.recipient_role, "allowed_sensitivity": st.session_state.allowed_sensitivity, "reviewer": st.session_state.reviewer, "publication_rule": "Approved claims only"})
    st.info("For production: add OAuth scopes, row-level access checks, redaction, audit persistence, retention controls and an immutable approval history.")


def inject_theme() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Fraunces:opsz,wght@9..144,600;9..144,700&display=swap');
        :root { --ink:#14352a; --muted:#60786b; --leaf:#2f7d52; --mint:#e6f2e9; --cream:#fbfaf5; --coral:#d77e57; }
        .stApp { background: radial-gradient(circle at 82% 4%, rgba(178,221,185,.42), transparent 29%), linear-gradient(135deg,#fbfaf5 0%,#f4f8f0 48%,#eef6ef 100%); color:var(--ink); }
        .stApp, .stApp p, .stApp label, .stApp [data-testid="stMarkdownContainer"] { font-family:'DM Sans', sans-serif; }
        h1,h2,h3,h4 { font-family:'Fraunces', Georgia, serif !important; color:var(--ink) !important; letter-spacing:-.02em; }
        .block-container { max-width: 1320px; padding: 2.4rem 3.2rem 4rem; }
        [data-testid="stSidebar"] { background:linear-gradient(180deg,#123f2d 0%,#1e583e 55%,#2b6b4a 100%); border-right:0; }
        [data-testid="stSidebar"] * { color:#eff8ef !important; }
        [data-testid="stSidebar"] .stCaption, [data-testid="stSidebar"] small { color:#cce5d1 !important; }
        [data-testid="stSidebar"] hr { border-color:rgba(255,255,255,.18); }
        [data-testid="stSidebar"] [data-testid="stFileUploader"] { border:1px dashed rgba(255,255,255,.45); border-radius:16px; padding:8px; }
        .hero { padding:2.1rem 2.4rem 2.25rem; border-radius:28px; background:linear-gradient(120deg,#173f2e 0%,#2e7d52 55%,#78ad75 100%); color:white; box-shadow:0 18px 45px rgba(26,83,54,.18); position:relative; overflow:hidden; margin-bottom:1.35rem; }
        .hero:after { content:'✦'; position:absolute; right:8%; top:-15%; font-size:15rem; line-height:1; color:rgba(255,255,255,.1); transform:rotate(16deg); }
        .hero .eyebrow { text-transform:uppercase; letter-spacing:.16em; font-size:.72rem; font-weight:700; color:#d9efd9; }
        .hero h1 { color:white !important; font-size:clamp(2.3rem,4vw,4.6rem); margin:.35rem 0 .45rem; max-width:780px; }
        .hero p { color:#e5f5e6; font-size:1.08rem; max-width:700px; margin:0; }
        .section-kicker { color:var(--leaf); font-size:.72rem; font-weight:700; letter-spacing:.16em; margin-top:1.2rem; }
        .section-title { margin:.1rem 0 .35rem; font-size:2.15rem; }
        .section-title.small { font-size:1.55rem; }
        .muted { color:var(--muted); }
        .empty-state { background:rgba(255,255,255,.66); border:1px solid #d8e7da; border-radius:22px; padding:2.4rem; text-align:center; margin-top:1rem; box-shadow:0 12px 28px rgba(27,73,45,.06); }
        .empty-state.compact { padding:2rem; }
        .empty-icon { color:var(--coral); font-size:2.2rem; margin-bottom:.4rem; }
        [data-testid="stMetric"] { background:rgba(255,255,255,.72); border:1px solid #dce9de; border-radius:16px; padding:1rem 1.1rem; box-shadow:0 9px 22px rgba(34,77,48,.06); }
        [data-testid="stMetricLabel"] { color:var(--muted); }
        [data-testid="stMetricValue"] { color:var(--ink); font-family:'Fraunces', Georgia, serif; }
        .stButton > button, .stDownloadButton > button { border-radius:12px; border:1px solid #c9dfcd; background:#ffffff; color:var(--ink); font-weight:700; transition:transform .18s ease, box-shadow .18s ease, background .18s ease; }
        .stButton > button:hover, .stDownloadButton > button:hover { transform:translateY(-2px); box-shadow:0 9px 18px rgba(29,91,52,.14); border-color:#77ad83; }
        [data-testid="stSidebar"] .stButton > button { background:#dff0df; color:#17482f !important; border:0; }
        [data-testid="stSidebar"] .stButton > button:hover { background:#f4fff2; }
        [data-testid="stTabs"] button { color:var(--muted); font-weight:700; }
        [data-testid="stTabs"] button[aria-selected="true"] { color:var(--leaf); }
        [data-testid="stTabs"] [data-baseweb="tab-highlight"] { background:var(--coral); }
        [data-testid="stExpander"], [data-testid="stVerticalBlockBorderWrapper"] { background:rgba(255,255,255,.65); border:1px solid #dce9de; border-radius:16px; }
        [data-testid="stProgressBar"] > div > div { background:linear-gradient(90deg,var(--leaf),#9bcf86); }
        .stAlert { border-radius:14px; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="✦", layout="wide", initial_sidebar_state="expanded")
    init_state()
    inject_theme()
    render_sidebar()
    st.markdown("<section class='hero'><div class='eyebrow'>PROJECT CONTINUITY STUDIO</div><h1>Make the invisible work visible.</h1><p>Turn scattered project activity into a traceable, reviewable handover — with the evidence, uncertainty, and next steps still attached.</p></section>", unsafe_allow_html=True)
    tabs = st.tabs(["Overview", "Evidence", "Review", "Handover", "Access"])
    with tabs[0]:
        render_overview()
    with tabs[1]:
        render_evidence()
    with tabs[2]:
        render_review()
    with tabs[3]:
        render_handover()
    with tabs[4]:
        render_access()


if __name__ == "__main__":
    main()
