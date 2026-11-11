from __future__ import annotations

import io
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
VERSION = "0.4 vertical slice"
CATEGORIES = ["Decision", "Question", "Risk", "Responsibility", "Dependency", "Next action"]
STATUS_OPTIONS = ["Draft", "Approved", "Needs evidence", "Conflicting", "Rejected"]
CONFIDENCE_OPTIONS = ["High", "Medium", "Low"]
SENSITIVITY_OPTIONS = ["Project", "Restricted", "Confidential"]
RECIPIENTS = {
    "Incoming project member": {"allowed": ["Project"], "description": "Role-filtered briefing for the person taking over."},
    "Project lead / manager": {"allowed": ["Project", "Restricted"], "description": "Continuity view with operational risks and restricted project context."},
    "Security reviewer": {"allowed": ["Project", "Restricted", "Confidential"], "description": "Full audit and access-policy review."},
}


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
    permission_scope: str = "Project workspace"
    freshness: str = "Current"


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
    due_date: str = ""
    conflict_flag: bool = False
    missing_evidence: bool = False
    source_freshness: str = "Current"


@dataclass
class AuditEvent:
    timestamp: str
    actor: str
    action: str
    target: str
    detail: str


SAMPLE_EVIDENCE = [
    Evidence("E001", "Kickoff notes", "Meeting note", "The team agreed to ship the pilot with manual approval before adding automated publishing. The goal is to reduce transition time without exposing restricted project knowledge.", "p. 1", "https://example.com/kickoff", "Maya", "2026-10-04"),
    Evidence("E002", "Engineering notes", "Repository note", "Decision: use PostgreSQL and pgvector for the evidence store. We should revisit this after the first 10,000 records.", "README.md", "https://github.com/example/project/blob/main/README.md", "Arun", "2026-10-10"),
    Evidence("E003", "Weekly sync", "Meeting note", "The Drive connector is blocked by missing OAuth scopes. This is the main launch risk and may delay the demo.", "p. 2", "https://example.com/weekly-sync", "Jiya", "2026-10-18"),
    Evidence("E004", "Task board export", "Task board", "Next action: Arun owns the connector retry flow. Jiya will prepare the reviewer walkthrough by Friday.", "TASK-42", "https://linear.app/example/issue/TASK-42", "Project bot", "2026-10-20"),
    Evidence("E005", "Project chat", "Chat", "Open question: should restricted HR notes be excluded at ingestion or only redacted at publication? Security needs to confirm.", "#handover", "https://chat.example.com/handover/123", "Sam", "2026-10-21", "Restricted", "Security group", "Current"),
    Evidence("E006", "Platform notes", "Repository note", "The launch depends on the identity provider configuration being approved by the platform team.", "platform.md", "https://github.com/example/project/blob/main/platform.md", "Sam", "2026-10-22"),
    Evidence("E007", "Pilot decision update", "Meeting note", "Later update: automated publishing is now approved for the pilot, replacing the earlier manual approval decision. The old kickoff note should not be treated as current.", "p. 3", "https://example.com/pilot-update", "Maya", "2026-10-26"),
    Evidence("E008", "Ownership thread", "Chat", "Arun owns the connector retry flow. A later message says Priya will own the same flow after the handover, so ownership needs confirmation.", "#engineering", "https://chat.example.com/engineering/789", "Priya", "2026-10-27"),
    Evidence("E009", "People map", "Contact list", "Important contacts: Sam for platform identity, Maya for product decisions, and the security group for restricted-source approvals.", "contacts.csv", "https://example.com/contacts", "Project lead", "2026-10-28"),
    Evidence("E010", "Retrospective", "Meeting note", "Unresolved question: who will monitor stale claims after the handover? No owner or due date has been recorded yet.", "p. 4", "https://example.com/retro", "Jiya", "2026-10-29"),
]


def init_state() -> None:
    defaults = {
        "evidence": [], "claims": [], "audit_log": [], "versions": [], "gaps": [],
        "analysis_ran": False, "analysis_notice": "", "llm_used": False, "use_llm": False,
        "project_name": "Project Handover", "project_goal": "Make project context transferable without exposing unauthorised knowledge.",
        "project_status": "In transition", "reviewer": "Jiya Joyardar", "recipient_role": "Incoming project member",
        "allowed_sensitivity": ["Project"], "selected_categories": CATEGORIES.copy(), "workflow_stage": 1,
        "share_expiry": "No expiry", "evidence_search": "", "global_search": "", "review_filter": "All", "review_confidence": "All",
        "chat_history": [], "analysis_timestamps": [], "last_backup": "",
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def now_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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


def record_event(action: str, target: str, detail: str) -> None:
    st.session_state.audit_log.insert(0, AuditEvent(now_label(), st.session_state.reviewer, action, target, detail))
    persist_snapshot()


def load_sample() -> None:
    st.session_state.evidence = [Evidence(**asdict(item)) for item in SAMPLE_EVIDENCE]
    st.session_state.claims = []
    st.session_state.gaps = []
    st.session_state.analysis_ran = False
    st.session_state.analysis_notice = ""
    st.session_state.workflow_stage = 1
    record_event("Loaded sample workspace", st.session_state.project_name, "10 synthetic source records loaded for demonstration.")


def clear_workspace() -> None:
    st.session_state.evidence = []
    st.session_state.claims = []
    st.session_state.gaps = []
    st.session_state.chat_history = []
    st.session_state.analysis_ran = False
    st.session_state.analysis_notice = "Workspace cleared. Add authorised evidence to begin a new handover."
    st.session_state.workflow_stage = 1
    record_event("Started fresh workspace", st.session_state.project_name, "Previous evidence, claims, and chat context cleared from the active session.")


def read_upload(uploaded_file) -> list[Evidence]:
    raw = uploaded_file.getvalue()
    filename = uploaded_file.name
    source_type = detect_type(filename)
    text = ""
    rows: list[dict[str, str]] = []
    if filename.lower().endswith(".pdf"):
        if PdfReader is None:
            return []
        reader = PdfReader(io.BytesIO(raw))
        for page_no, page in enumerate(reader.pages, start=1):
            page_text = clean_text(page.extract_text())
            if page_text:
                rows.append({"text": page_text, "locator": f"page {page_no}"})
    elif filename.lower().endswith(".docx"):
        if Document is None:
            return []
        document = Document(io.BytesIO(raw))
        text = "\n".join(p.text for p in document.paragraphs)
    elif filename.lower().endswith(".csv"):
        df = pd.read_csv(io.BytesIO(raw))
        text = "\n".join(" | ".join(f"{column}: {row[column]}" for column in df.columns) for _, row in df.iterrows())
    elif filename.lower().endswith(".json"):
        text = json.dumps(json.loads(raw.decode("utf-8", errors="ignore")), indent=2, ensure_ascii=False)
    else:
        text = raw.decode("utf-8", errors="ignore")
    if text:
        rows = [{"text": text, "locator": "uploaded file"}]
    evidence: list[Evidence] = []
    for index, row in enumerate(rows, start=1):
        evidence.append(Evidence(
            evidence_id=f"UP{len(st.session_state.evidence) + index:03d}", source_name=filename,
            source_type=source_type, text=clean_text(row["text"]), locator=row.get("locator", ""),
        ))
    return evidence


def visible_evidence() -> list[Evidence]:
    allowed = set(st.session_state.allowed_sensitivity)
    return [item for item in st.session_state.evidence if item.sensitivity in allowed]


@st.cache_data(show_spinner=False, ttl=900)
def cached_heuristic_extract(evidence_payload: str) -> list[dict[str, Any]]:
    """Cache deterministic extraction by content, not by mutable session objects."""
    records = [Evidence(**row) for row in json.loads(evidence_payload)]
    return [asdict(claim) for claim in heuristic_extract(records)]


def analysis_rate_allowed() -> bool:
    """Session-level guard; production deployments should use Redis at the edge."""
    now = time.time()
    recent = [stamp for stamp in st.session_state.analysis_timestamps if now - stamp < 60]
    st.session_state.analysis_timestamps = recent
    if len(recent) >= 8:
        st.session_state.analysis_notice = "Rate limit reached: please wait a minute before running another reconstruction."
        return False
    st.session_state.analysis_timestamps.append(now)
    return True


def add_claim(claims: list[Claim], category: str, text: str, evidence: Evidence, confidence: str = "Medium", owner: str = "") -> None:
    text = clean_text(text)
    if len(text) < 18:
        return
    key = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if any(c.category == category and re.sub(r"[^a-z0-9]+", " ", c.text.lower()).strip() == key for c in claims):
        return
    due_date = ""
    date_match = re.search(r"(?:by|before|on)\s+([A-Za-z]+\s+\d{1,2}|Friday|Monday|Tuesday|Wednesday|Thursday)", text, re.I)
    if date_match:
        due_date = date_match.group(1)
    claims.append(Claim(
        claim_id=f"C{len(claims) + 1:03d}", category=category, text=text, evidence_ids=[evidence.evidence_id],
        confidence=confidence, owner=owner, due_date=due_date, source_freshness=evidence.freshness,
    ))


def heuristic_extract(evidence_items: Iterable[Evidence]) -> list[Claim]:
    claims: list[Claim] = []
    for evidence in evidence_items:
        for sentence in split_sentences(evidence.text):
            lower = sentence.lower()
            if "?" in sentence or any(token in lower for token in ["open question", "unresolved question", "unknown", "need to confirm", "needs to confirm"]):
                add_claim(claims, "Question", sentence, evidence, "Medium")
            if any(token in lower for token in ["risk", "blocked", "blocker", "may delay", "at risk", "critical"]):
                add_claim(claims, "Risk", sentence, evidence, "High" if any(token in lower for token in ["blocked", "critical", "main launch risk"]) else "Medium")
            if any(token in lower for token in ["decision:", "decided", "agreed", "approved", "chosen"]):
                add_claim(claims, "Decision", sentence, evidence, "High" if any(token in lower for token in ["decision:", "agreed", "approved"]) else "Medium")
            if any(token in lower for token in ["owns", "owner", "responsible", "will prepare", "will lead", "will monitor"]):
                owner_match = re.search(r"([A-Z][a-z]+)\s+(?:owns|will|is responsible)", sentence)
                add_claim(claims, "Responsibility", sentence, evidence, "Medium", owner_match.group(1) if owner_match else "")
            if any(token in lower for token in ["depends on", "dependency", "requires", "blocked by"]):
                add_claim(claims, "Dependency", sentence, evidence, "Medium")
            if any(token in lower for token in ["next action", "todo", "to-do", "should ", "action:", "will prepare", "will monitor"]):
                add_claim(claims, "Next action", sentence, evidence, "Medium")
    return claims


def detect_conflicts(claims: list[Claim], evidence_items: list[Evidence]) -> None:
    all_text = " ".join(item.text.lower() for item in evidence_items)
    for claim in claims:
        lower = claim.text.lower()
        if ("manual approval" in all_text and "automated publishing" in all_text and claim.category == "Decision") or "replacing the earlier" in lower or "needs confirmation" in lower or "same flow" in lower:
            claim.conflict_flag = True
            claim.status = "Conflicting"
            claim.confidence = "Low"
        if claim.category in {"Question", "Responsibility", "Next action"} and any(token in lower for token in ["no owner", "no due date", "needs to confirm", "who will"]):
            claim.missing_evidence = True
            if claim.status == "Draft":
                claim.status = "Needs evidence"


def derive_gaps(claims: list[Claim]) -> list[str]:
    gaps = []
    for claim in claims:
        if claim.missing_evidence:
            gaps.append(f"{claim.category}: {claim.text}")
        elif claim.category == "Next action" and not claim.owner and not claim.due_date:
            gaps.append(f"Next action lacks owner and due date: {claim.text}")
    return list(dict.fromkeys(gaps))


def llm_extract(evidence_items: list[Evidence]) -> list[Claim]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return []
    client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_API_BASE")) if os.getenv("OPENAI_API_BASE") else OpenAI(api_key=api_key)
    evidence_text = "\n".join(f"[{e.evidence_id}] {e.text}" for e in evidence_items)
    prompt = f"Extract a project handover from this evidence. Return JSON {{'claims':[...]}}, with category, text, evidence_ids, confidence. Categories: {', '.join(CATEGORIES)}. Never invent facts; mark missing or conflicting evidence explicitly.\n\n{evidence_text}"
    try:
        response = client.chat.completions.create(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), temperature=0, response_format={"type": "json_object"}, messages=[{"role": "system", "content": "You are a careful evidence-grounded handover analyst."}, {"role": "user", "content": prompt}])
        payload = json.loads(response.choices[0].message.content)
        rows = payload if isinstance(payload, list) else payload.get("claims", [])
        claims = []
        for index, row in enumerate(rows, start=1):
            if row.get("category") in CATEGORIES and row.get("text") and row.get("evidence_ids"):
                claims.append(Claim(f"C{index:03d}", row["category"], clean_text(row["text"]), list(row["evidence_ids"]), row.get("confidence", "Medium")))
        return claims
    except Exception as exc:  # pragma: no cover
        st.warning(f"Optional AI extraction failed; deterministic extraction used instead. {exc}")
        return []


def run_analysis() -> None:
    if not analysis_rate_allowed():
        return
    filtered = visible_evidence()
    if not filtered:
        st.session_state.claims, st.session_state.gaps = [], ["No evidence is visible under the current access policy."]
        st.session_state.analysis_ran = True
        st.session_state.analysis_notice = "Analysis stopped: no authorised evidence is visible."
        return
    llm_claims = llm_extract(filtered) if st.session_state.use_llm else []
    evidence_payload = json.dumps([asdict(item) for item in filtered], sort_keys=True)
    cached_claim_rows = cached_heuristic_extract(evidence_payload)
    claims = llm_claims or [Claim(**row) for row in cached_claim_rows]
    detect_conflicts(claims, filtered)
    st.session_state.claims = claims
    st.session_state.gaps = derive_gaps(claims)
    st.session_state.analysis_ran = True
    st.session_state.llm_used = bool(llm_claims)
    st.session_state.workflow_stage = 3
    st.session_state.analysis_notice = f"Analysis complete — {len(claims)} traceable claims, {len(st.session_state.gaps)} evidence gaps, and {sum(c.conflict_flag for c in claims)} conflict flag(s)."
    record_event("Reconstructed project state", st.session_state.project_name, st.session_state.analysis_notice)


def evidence_lookup() -> dict[str, Evidence]:
    return {item.evidence_id: item for item in st.session_state.evidence}


def status_counts() -> dict[str, int]:
    return {status: sum(1 for claim in st.session_state.claims if claim.status == status) for status in STATUS_OPTIONS}


def health_score() -> int:
    if not st.session_state.claims:
        return 0
    total = len(st.session_state.claims)
    approved = sum(c.status == "Approved" for c in st.session_state.claims)
    penalties = sum(c.conflict_flag or c.missing_evidence for c in st.session_state.claims)
    return max(0, min(100, round((approved / total) * 100 - (penalties / total) * 25)))


def approve_supported() -> None:
    approved = 0
    for claim in st.session_state.claims:
        if not claim.conflict_flag and not claim.missing_evidence and claim.evidence_ids:
            claim.status = "Approved"
            approved += 1
    st.session_state.workflow_stage = 4
    record_event("Bulk approved supported claims", st.session_state.project_name, f"Approved {approved} claim(s); conflicting and evidence-gap items remain for review.")


def markdown_export(approved_only: bool = True) -> str:
    lookup = evidence_lookup()
    claims = [c for c in st.session_state.claims if c.status == "Approved" or not approved_only]
    lines = [f"# {st.session_state.project_name} — Handover", "", f"**Goal:** {st.session_state.project_goal}", f"**Status:** {st.session_state.project_status}", f"**Recipient:** {st.session_state.recipient_role}", f"**Reviewer:** {st.session_state.reviewer}", f"**Generated:** {now_label()}", "", "## Confidence and access note", "", "This handover includes only approved claims visible under the configured audience policy. Uncertainty and conflicts are intentionally excluded from the shareable view.", ""]
    for category in CATEGORIES:
        rows = [claim for claim in claims if claim.category == category and claim.status != "Rejected"]
        if not rows:
            continue
        lines += [f"## {category}", ""]
        for claim in rows:
            refs = []
            for evidence_id in claim.evidence_ids:
                evidence = lookup.get(evidence_id)
                if evidence:
                    label = f"{evidence.source_name} ({evidence.locator})".strip()
                    refs.append(f"[{label}]({evidence.source_url})" if evidence.source_url else label)
            lines.append(f"- **{claim.status} · {claim.confidence} confidence:** {claim.text} _(Evidence: {'; '.join(refs) or 'missing'})_")
        lines.append("")
    return "\n".join(lines)


def recovery_payload() -> str:
    payload = {
        "format": "handover-intelligence-recovery-v1",
        "saved_at": now_label(),
        "project": {key: st.session_state[key] for key in ["project_name", "project_goal", "project_status", "reviewer", "recipient_role", "allowed_sensitivity", "share_expiry"]},
        "evidence": [asdict(item) for item in st.session_state.evidence],
        "claims": [asdict(item) for item in st.session_state.claims],
        "gaps": st.session_state.gaps,
        "audit_log": [asdict(item) for item in st.session_state.audit_log],
        "versions": st.session_state.versions,
        "chat_history": st.session_state.chat_history,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def persist_snapshot() -> None:
    """Best-effort local recovery snapshot; mount /app/data in container deployments."""
    try:
        data_dir = Path(os.getenv("HANDOVER_DATA_DIR", "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = data_dir / "latest-recovery.json"
        snapshot_path.write_text(recovery_payload(), encoding="utf-8")
        st.session_state.last_backup = str(snapshot_path)
    except OSError:
        # The app still offers a browser download backup when the runtime filesystem is read-only.
        pass


def restore_recovery(uploaded_file) -> None:
    payload = json.loads(uploaded_file.getvalue().decode("utf-8"))
    if payload.get("format") != "handover-intelligence-recovery-v1":
        raise ValueError("This is not a Handover Intelligence recovery file.")
    project = payload.get("project", {})
    for key in ["project_name", "project_goal", "project_status", "reviewer", "recipient_role", "allowed_sensitivity", "share_expiry"]:
        if key in project:
            st.session_state[key] = project[key]
    st.session_state.evidence = [Evidence(**row) for row in payload.get("evidence", [])]
    st.session_state.claims = [Claim(**row) for row in payload.get("claims", [])]
    st.session_state.gaps = payload.get("gaps", [])
    st.session_state.audit_log = [AuditEvent(**row) for row in payload.get("audit_log", [])]
    st.session_state.versions = payload.get("versions", [])
    st.session_state.chat_history = payload.get("chat_history", [])
    st.session_state.analysis_ran = bool(st.session_state.claims)
    st.session_state.workflow_stage = 3 if st.session_state.claims else 1
    record_event("Restored recovery backup", st.session_state.project_name, f"Restored {len(st.session_state.evidence)} evidence item(s) and {len(st.session_state.claims)} claim(s).")


def inject_theme() -> None:
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Fraunces:opsz,wght@9..144,600;9..144,700&display=swap');
    :root{--ink:#14352a;--muted:#60786b;--leaf:#2f7d52;--mint:#e6f2e9;--cream:#fbfaf5;--coral:#d77e57;--gold:#d2a84c;}
    .stApp{background:radial-gradient(circle at 82% 4%,rgba(178,221,185,.42),transparent 29%),linear-gradient(135deg,#fbfaf5 0%,#f4f8f0 48%,#eef6ef 100%);color:var(--ink)}
    .stApp,.stApp p,.stApp label,.stApp [data-testid="stMarkdownContainer"]{font-family:'DM Sans',sans-serif}
    h1,h2,h3,h4{font-family:'Fraunces',Georgia,serif!important;color:var(--ink)!important;letter-spacing:-.02em}
    .block-container{max-width:1380px;padding:2.2rem 3.2rem 4rem}
    [data-testid="stSidebar"]{background:linear-gradient(180deg,#123f2d 0%,#1e583e 55%,#2b6b4a 100%);border-right:0}
    [data-testid="stSidebar"] *{color:#eff8ef!important}[data-testid="stSidebar"] .stCaption,[data-testid="stSidebar"] small{color:#cce5d1!important}[data-testid="stSidebar"] hr{border-color:rgba(255,255,255,.18)}
    .hero{padding:2.1rem 2.4rem 2.25rem;border-radius:28px;background:linear-gradient(120deg,#173f2e 0%,#2e7d52 55%,#78ad75 100%);color:white;box-shadow:0 18px 45px rgba(26,83,54,.18);position:relative;overflow:hidden;margin-bottom:1.25rem}.hero:after{content:'✦';position:absolute;right:8%;top:-15%;font-size:15rem;line-height:1;color:rgba(255,255,255,.1);transform:rotate(16deg)}.hero .eyebrow{text-transform:uppercase;letter-spacing:.16em;font-size:.72rem;font-weight:700;color:#d9efd9}.hero h1{color:white!important;font-size:clamp(2.3rem,4vw,4.6rem);margin:.35rem 0 .45rem;max-width:780px}.hero p{color:#e5f5e6;font-size:1.08rem;max-width:700px;margin:0}
    .section-kicker{color:var(--leaf);font-size:.72rem;font-weight:700;letter-spacing:.16em;margin-top:1.2rem}.section-title{margin:.1rem 0 .35rem;font-size:2.15rem}.section-title.small{font-size:1.55rem}.muted{color:var(--muted)}
    .empty-state{background:rgba(255,255,255,.66);border:1px solid #d8e7da;border-radius:22px;padding:2.4rem;text-align:center;margin-top:1rem;box-shadow:0 12px 28px rgba(27,73,45,.06)}.empty-state.compact{padding:2rem}.empty-icon{color:var(--coral);font-size:2.2rem;margin-bottom:.4rem}
    [data-testid="stMetric"]{background:rgba(255,255,255,.72);border:1px solid #dce9de;border-radius:16px;padding:1rem 1.1rem;box-shadow:0 9px 22px rgba(34,77,48,.06)}[data-testid="stMetricLabel"]{color:var(--muted)}[data-testid="stMetricValue"]{color:var(--ink);font-family:'Fraunces',Georgia,serif}
    .stButton>button,.stDownloadButton>button{border-radius:12px;border:1px solid #c9dfcd;background:#fff;color:var(--ink);font-weight:700;transition:transform .18s ease,box-shadow .18s ease,background .18s ease}.stButton>button:hover,.stDownloadButton>button:hover{transform:translateY(-2px);box-shadow:0 9px 18px rgba(29,91,52,.14);border-color:#77ad83}[data-testid="stSidebar"] .stButton>button,[data-testid="stSidebar"] .stButton>button p,[data-testid="stSidebar"] .stDownloadButton>button,[data-testid="stSidebar"] .stDownloadButton>button p{background:#dff0df;color: #000 !important;border:0}[data-testid="stSidebar"] .stButton>button:hover,[data-testid="stSidebar"] .stDownloadButton>button:hover{background:#f4fff2;color:#000!important}
    [data-testid="stTabs"] button{color:var(--muted);font-weight:700}[data-testid="stTabs"] button[aria-selected="true"]{color:var(--leaf)}[data-testid="stTabs"] [data-baseweb="tab-highlight"]{background:var(--coral)}
    [data-testid="stExpander"],[data-testid="stVerticalBlockBorderWrapper"]{background:rgba(255,255,255,.65);border:1px solid #dce9de;border-radius:16px}[data-testid="stProgressBar"]>div>div{background:linear-gradient(90deg,var(--leaf),#9bcf86)}.stAlert{border-radius:14px}
    .stage-strip{display:flex;gap:6px;margin:0 0 1.3rem}.stage{flex:1;padding:.58rem .4rem;border-radius:10px;text-align:center;font-size:.72rem;font-weight:700;border:1px solid #d8e6da;color:#6b8174;background:rgba(255,255,255,.48)}.stage.active{color:white;background:var(--leaf);border-color:var(--leaf)}.stage.done{color:#25613e;background:#dff0df;border-color:#b8d8bd}
    .source-chip{display:inline-block;padding:.25rem .55rem;border-radius:99px;background:#e2f0e4;color:#286645;font-size:.73rem;font-weight:700;margin:.15rem .15rem 0 0}.risk-chip{background:#f8e6d9;color:#98512e}.conflict-chip{background:#f7edc7;color:#80651a}.restricted-chip{background:#eadff0;color:#6b4875}
    .architecture{background:#14352a;color:#eff8ef;border-radius:18px;padding:1.2rem 1.35rem}.architecture h4{color:#e4f4e4!important;margin:.1rem 0 .45rem}.architecture p{color:#c7e0cc;font-size:.9rem}.arrow{text-align:center;color:#d6bb62;font-size:1.5rem;padding:.35rem}
    </style>
    """, unsafe_allow_html=True)


def render_sidebar() -> None:
    with st.sidebar:
        st.markdown("<div style='font-size:.72rem;letter-spacing:.18em;font-weight:700;color:#b9dfbf!important'>HANDOVER INTELLIGENCE</div><h2 style='color:white!important;margin:.2rem 0 1.2rem'>Control room</h2>", unsafe_allow_html=True)
        st.caption(VERSION)
        st.markdown("**1 · Define the handover**")
        st.session_state.project_name = st.text_input("Project name", st.session_state.project_name)
        st.session_state.project_goal = st.text_area("Project goal", st.session_state.project_goal, height=76)
        st.session_state.project_status = st.selectbox("Project status", ["In transition", "At risk", "Stable", "Completed"], index=["In transition", "At risk", "Stable", "Completed"].index(st.session_state.project_status))
        st.session_state.recipient_role = st.selectbox("Recipient audience", list(RECIPIENTS), index=list(RECIPIENTS).index(st.session_state.recipient_role))
        st.session_state.reviewer = st.text_input("Reviewer", st.session_state.reviewer)
        st.divider()
        st.markdown("**2 · Connect authorised sources**")
        connector = st.selectbox("Source connector", ["Upload workspace export", "GitHub repository", "Google Drive / Docs", "Meeting notes", "Task board", "Chat export"], label_visibility="collapsed")
        uploaded = st.file_uploader("Drop source files", type=["txt", "md", "csv", "json", "pdf", "docx"], accept_multiple_files=True)
        if uploaded and st.button("Add source evidence", use_container_width=True):
            additions = [item for file in uploaded for item in read_upload(file)]
            for item in additions:
                item.permission_scope = connector
            st.session_state.evidence.extend(additions)
            st.session_state.analysis_ran = False
            record_event("Added authorised source", connector, f"Added {len(additions)} evidence item(s).")
            st.session_state.analysis_notice = f"Added {len(additions)} source item(s). Reconstruct the project state to refresh claims."
            st.rerun()
        if st.button("Load sample workspace", use_container_width=True):
            load_sample()
            st.success("Loaded 10 synthetic source records.")
        if st.button("Start fresh workspace", use_container_width=True):
            clear_workspace()
            st.success("Workspace cleared. No sample sources will be analysed.")
        st.download_button("Download recovery backup", recovery_payload(), file_name="handover-recovery.json", mime="application/json", use_container_width=True)
        recovery_file = st.file_uploader("Restore recovery backup", type=["json"], key="recovery_upload", label_visibility="collapsed")
        if recovery_file and st.button("Restore saved workspace", use_container_width=True):
            try:
                restore_recovery(recovery_file)
                st.success("Workspace restored safely.")
                st.rerun()
            except (ValueError, json.JSONDecodeError, TypeError) as exc:
                st.error(f"Restore failed safely: {exc}")
        st.divider()
        st.markdown("**3 · Publication policy**")
        recipient_policy = RECIPIENTS[st.session_state.recipient_role]
        st.caption(recipient_policy["description"])
        st.session_state.allowed_sensitivity = st.multiselect("Allowed sensitivity", SENSITIVITY_OPTIONS, default=st.session_state.allowed_sensitivity)
        st.session_state.share_expiry = st.selectbox("Share link expiry", ["No expiry", "7 days", "30 days"], index=["No expiry", "7 days", "30 days"].index(st.session_state.share_expiry))
        st.session_state.use_llm = st.toggle("Use optional AI extraction", value=st.session_state.use_llm, help="Requires OPENAI_API_KEY. Deterministic extraction remains available without it.")
        if st.button("Reconstruct project state →", type="primary", use_container_width=True):
            run_analysis()
            st.success(st.session_state.analysis_notice)


def render_stage_strip() -> None:
    labels = ["Collect", "Reconstruct", "Review", "Approve", "Share"]
    html = "<div class='stage-strip'>"
    for index, label in enumerate(labels, start=1):
        css = "active" if index == st.session_state.workflow_stage else ("done" if index < st.session_state.workflow_stage else "")
        html += f"<div class='stage {css}'>{index:02d} · {label}</div>"
    st.markdown(html + "</div>", unsafe_allow_html=True)


def render_global_search() -> None:
    st.markdown("<div class='section-kicker'>SEARCH THE PROJECT MEMORY</div><h3 class='section-title small'>Find a decision, person, risk, or keyword</h3>", unsafe_allow_html=True)
    query = st.text_input("Search uploaded evidence", placeholder="Search across PDF, DOCX, TXT, Markdown, CSV, or JSON uploads…", key="global_search")
    if not query:
        st.caption("The search runs across every authorised, extracted source record. Restricted sources stay hidden by policy.")
        return
    terms = [term for term in re.findall(r"\w+", query.lower()) if len(term) > 2]
    results = []
    for item in visible_evidence():
        haystack = f"{item.source_name} {item.text}".lower()
        score = sum(term in haystack for term in terms)
        if score:
            results.append((score, item))
    results.sort(key=lambda pair: pair[0], reverse=True)
    st.caption(f"{len(results)} matching source(s) · searched {len(visible_evidence())} authorised source(s)")
    if not results:
        st.warning("No matching evidence found. Try a shorter keyword or a source name.")
        return
    for score, item in results[:8]:
        snippet = item.text[:420] + ("…" if len(item.text) > 420 else "")
        st.markdown(f"<div style='padding:.75rem 0;border-bottom:1px solid #dce9de'><span class='source-chip'>{item.evidence_id}</span> <b>{item.source_name}</b> <span class='muted'>· {score} keyword hit(s) · {item.locator or 'source record'}</span><br>{snippet}</div>", unsafe_allow_html=True)


def render_overview() -> None:
    st.markdown("<div class='section-kicker'>COMMAND CENTER</div><h2 class='section-title'>A handover that knows what it knows</h2><p class='muted'>Reconstruct the project state, make uncertainty visible, then publish only what a human reviewer can defend.</p>", unsafe_allow_html=True)
    render_stage_strip()
    if not st.session_state.evidence:
        st.markdown("<div class='empty-state'><div class='empty-icon'>✦</div><h3>Start with authorised evidence</h3><p>Load the sample workspace to see the full winning-demo flow, or add your own meeting notes and task exports from the control room.</p></div>", unsafe_allow_html=True)
        return
    render_global_search()
    if st.session_state.analysis_notice:
        st.success(st.session_state.analysis_notice)
    action_cols = st.columns([1.35, 3.65])
    with action_cols[0]:
        if st.button("Reconstruct now →", type="primary", use_container_width=True, key="overview_analyse"):
            run_analysis()
            st.success(st.session_state.analysis_notice)
    with action_cols[1]:
        st.caption("The system never shares generated text automatically. Review, correct, approve, then publish for a named audience.")
    counts = status_counts()
    kpi = st.columns(6)
    kpi[0].metric("Sources", len(visible_evidence()))
    kpi[1].metric("Claims", len(st.session_state.claims))
    kpi[2].metric("Approved", counts["Approved"])
    kpi[3].metric("Conflicts", sum(c.conflict_flag for c in st.session_state.claims))
    kpi[4].metric("Evidence gaps", len(st.session_state.gaps))
    kpi[5].metric("Health score", f"{health_score()}/100")
    if not st.session_state.analysis_ran:
        st.warning("Sources are loaded. Reconstruct the project state to create the review queue.")
        return
    st.markdown("<div class='section-kicker'>WHAT NEEDS ATTENTION</div><h3 class='section-title small'>Signals for the incoming person</h3>", unsafe_allow_html=True)
    left, right = st.columns([1.15, 1])
    with left:
        risk_claims = [c for c in st.session_state.claims if c.category == "Risk" or c.conflict_flag]
        if risk_claims:
            for claim in risk_claims[:5]:
                chip = "Conflict" if claim.conflict_flag else "Risk"
                st.markdown(f"<div style='padding:.75rem 0;border-bottom:1px solid #dce9de'><span class='source-chip risk-chip'>{chip}</span> <b>{claim.text}</b><br><span class='muted'>Evidence: {', '.join(claim.evidence_ids)} · {claim.confidence} confidence</span></div>", unsafe_allow_html=True)
        else:
            st.success("No high-signal risks detected in the visible evidence.")
    with right:
        st.markdown("**Review readiness**")
        reviewed = sum(c.status in {"Approved", "Rejected"} for c in st.session_state.claims)
        readiness = round(100 * reviewed / len(st.session_state.claims)) if st.session_state.claims else 0
        st.progress(readiness / 100, text=f"{readiness}% reviewed")
        st.caption("The score rewards supported, approved knowledge and penalises conflict or missing evidence.")
        if st.session_state.gaps:
            st.markdown("**Evidence gaps**")
            for gap in st.session_state.gaps[:4]:
                st.markdown(f"- {gap}")


def render_evidence() -> None:
    st.markdown("<div class='section-kicker'>SOURCE ROOM</div><h2 class='section-title'>Evidence vault</h2><p class='muted'>Every claim starts with a source. Search the trail, inspect context, and follow the link back.</p>", unsafe_allow_html=True)
    if not st.session_state.evidence:
        st.info("No source evidence loaded yet.")
        return
    search = st.text_input("Search evidence", placeholder="Try: OAuth, Arun, launch risk, decision, E001…", key="evidence_search")
    source_items = visible_evidence()
    if search:
        source_items = [e for e in source_items if search.lower() in e.text.lower() or search.lower() in e.source_name.lower() or search.lower() in e.evidence_id.lower()]
        st.caption(f"Showing {len(source_items)} matching source(s) out of {len(visible_evidence())} visible source(s).")
        if not source_items:
            st.markdown("<div class='empty-state compact'><div class='empty-icon'>⌕</div><h3>No matching evidence</h3><p>Try a broader keyword, source name, or evidence ID such as <b>E001</b>.</p></div>", unsafe_allow_html=True)
            return
    else:
        st.caption(f"Showing all {len(source_items)} visible source(s). Restricted sources hidden: {len(st.session_state.evidence) - len(visible_evidence())}.")
    for evidence in source_items:
        sensitivity_class = "restricted-chip" if evidence.sensitivity != "Project" else ""
        with st.expander(f"{evidence.evidence_id} · {evidence.source_name} · {evidence.source_type}"):
            st.markdown(f"<span class='source-chip {sensitivity_class}'>{evidence.sensitivity}</span> <span class='source-chip'>{evidence.freshness}</span> <span class='source-chip'>{evidence.permission_scope}</span>", unsafe_allow_html=True)
            st.write(evidence.text)
            cols = st.columns(4)
            cols[0].caption(f"Locator: {evidence.locator or '—'}")
            cols[1].caption(f"Author: {evidence.author or '—'}")
            cols[2].caption(f"Timestamp: {evidence.timestamp or '—'}")
            cols[3].caption(f"Access: {evidence.permission_scope}")
            if evidence.source_url:
                st.link_button("Open supporting source", evidence.source_url)


def render_review() -> None:
    st.markdown("<div class='section-kicker'>HUMAN IN THE LOOP</div><h2 class='section-title'>Review, correct, approve</h2><p class='muted'>The model proposes. The reviewer decides. Every claim keeps its source trail, uncertainty, and publication state.</p>", unsafe_allow_html=True)
    if not st.session_state.claims:
        st.markdown("<div class='empty-state compact'><div class='empty-icon'>◎</div><h3>Nothing to review yet</h3><p>Load the sample workspace, then click <b>Reconstruct project state →</b> in the control room.</p></div>", unsafe_allow_html=True)
        return
    toolbar = st.columns([1.4, 1.25, 1.1, 1.4])
    st.session_state.review_filter = toolbar[0].selectbox("Review state", ["All"] + STATUS_OPTIONS, index=(["All"] + STATUS_OPTIONS).index(st.session_state.review_filter), key="review_state_select")
    st.session_state.review_confidence = toolbar[1].selectbox("Confidence", ["All"] + CONFIDENCE_OPTIONS, index=(["All"] + CONFIDENCE_OPTIONS).index(st.session_state.review_confidence), key="review_confidence_select")
    category_filter = toolbar[2].selectbox("Category", ["All"] + CATEGORIES, key="review_category_select")
    if toolbar[3].button("Approve supported claims", use_container_width=True):
        approve_supported()
        st.success("Supported claims approved. Conflicts and evidence gaps remain visible for human review.")
    lookup = evidence_lookup()
    visible_claims = [c for c in st.session_state.claims if (st.session_state.review_filter == "All" or c.status == st.session_state.review_filter) and (st.session_state.review_confidence == "All" or c.confidence == st.session_state.review_confidence) and (category_filter == "All" or c.category == category_filter)]
    st.caption(f"Showing {len(visible_claims)} of {len(st.session_state.claims)} generated claims.")
    for claim in visible_claims:
        with st.container(border=True):
            top = st.columns([1.45, 1.0, 1.2, 1.5])
            top[0].markdown(f"**{claim.claim_id} · {claim.category}**")
            chips = []
            if claim.conflict_flag: chips.append("<span class='source-chip conflict-chip'>Conflict</span>")
            if claim.missing_evidence: chips.append("<span class='source-chip risk-chip'>Needs evidence</span>")
            top[1].markdown(" ".join(chips) or f"{claim.confidence} confidence", unsafe_allow_html=True)
            new_status = top[2].selectbox("Status", STATUS_OPTIONS, index=STATUS_OPTIONS.index(claim.status), key=f"status_{claim.claim_id}", label_visibility="collapsed")
            top[3].markdown(" · ".join(claim.evidence_ids) if claim.evidence_ids else "Missing evidence")
            edited = st.text_area("Claim", claim.text, key=f"text_{claim.claim_id}", height=76)
            note = st.text_input("Reviewer note / correction rationale", claim.reviewer_note, key=f"note_{claim.claim_id}")
            owner_col, due_col = st.columns(2)
            owner = owner_col.text_input("Owner", claim.owner, key=f"owner_{claim.claim_id}")
            due = due_col.text_input("Due / freshness note", claim.due_date, key=f"due_{claim.claim_id}")
            if edited != claim.text or new_status != claim.status or note != claim.reviewer_note or owner != claim.owner or due != claim.due_date:
                claim.text, claim.status, claim.reviewer_note, claim.owner, claim.due_date = edited, new_status, note, owner, due
                record_event("Edited claim", claim.claim_id, f"Status: {new_status}; reviewer correction captured.")
            sources = [lookup[eid] for eid in claim.evidence_ids if eid in lookup]
            if sources:
                st.caption("Supporting evidence: " + " | ".join(f"{item.evidence_id}: {item.source_name} — {item.text[:180]}" for item in sources))
            else:
                st.error("This claim has no accessible supporting evidence.")


def render_handover() -> None:
    st.markdown("<div class='section-kicker'>READY TO PASS ON</div><h2 class='section-title'>Audience-safe handover pack</h2><p class='muted'>A calm, role-filtered view for the person taking the next step. Only approved claims are shareable.</p>", unsafe_allow_html=True)
    approved = [claim for claim in st.session_state.claims if claim.status == "Approved"]
    if not st.session_state.claims:
        st.info("Run reconstruction first to create a handover pack.")
        return
    header = st.columns([3, 1.2])
    header[0].markdown(f"### {st.session_state.project_name}\n**Goal:** {st.session_state.project_goal}\n\n**Audience:** {st.session_state.recipient_role} · **Expiry:** {st.session_state.share_expiry}")
    header[1].metric("Approved claims", len(approved))
    if not approved:
        st.warning("The pack is still in review. Approve supported claims before sharing.")
    if approved:
        for category in CATEGORIES:
            rows = [claim for claim in approved if claim.category == category]
            if rows:
                st.markdown(f"#### {category}")
                for claim in rows:
                    st.markdown(f"- {claim.text}\n  - <span class='source-chip'>{claim.confidence} confidence</span> <span class='source-chip'>Evidence: {', '.join(claim.evidence_ids)}</span>", unsafe_allow_html=True)
        st.divider()
        st.markdown("**Publication gate**")
        st.caption("Before sharing: re-check recipient identity, source permissions, redaction preview, expiry, and approval history.")
        if st.button("Approve handover for sharing →", type="primary"):
            st.session_state.workflow_stage = 5
            record_event("Approved handover for sharing", st.session_state.recipient_role, f"Audience: {st.session_state.recipient_role}; expiry: {st.session_state.share_expiry}.")
            version = {"version": len(st.session_state.versions) + 1, "timestamp": now_label(), "recipient": st.session_state.recipient_role, "claims": len(approved)}
            st.session_state.versions.insert(0, version)
            st.success("Handover approved. The shareable view contains approved claims only.")
        dl1, dl2 = st.columns(2)
        dl1.download_button("Download approved handover (.md)", markdown_export(True), file_name="approved-handover.md", mime="text/markdown", type="primary")
        dl2.download_button("Download audit packet (.json)", json.dumps({"project": st.session_state.project_name, "recipient": st.session_state.recipient_role, "claims": [asdict(item) for item in approved], "evidence": [asdict(item) for item in visible_evidence()], "audit": [asdict(item) for item in st.session_state.audit_log]}, indent=2), file_name="handover-audit.json", mime="application/json")


def answer_from_evidence(question: str) -> tuple[str, list[str]]:
    terms = [term for term in re.findall(r"\w+", question.lower()) if len(term) > 2]
    ranked = []
    for item in visible_evidence():
        haystack = f"{item.source_name} {item.text}".lower()
        score = sum(term in haystack for term in terms)
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    if not ranked:
        return "I could not find supporting evidence in the authorised project sources. I will not guess. Try a source name, person, risk, or decision keyword.", []
    selected = [item for _, item in ranked[:3]]
    answer = "Based on the authorised evidence, here is the strongest available context:\n\n" + "\n".join(f"- {item.text}" for item in selected)
    return answer, [item.evidence_id for item in selected]


def render_copilot() -> None:
    st.markdown("<div class='section-kicker'>EVIDENCE-GROUNDED COPILOT</div><h2 class='section-title'>Ask the project memory</h2><p class='muted'>This chat answers only from authorised evidence and shows the source IDs used. It refuses to invent missing context.</p>", unsafe_allow_html=True)
    if not visible_evidence():
        st.info("Load or upload evidence before asking the copilot a question.")
        return
    for message in st.session_state.chat_history:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message.get("sources"):
                st.caption("Sources: " + ", ".join(message["sources"]))
    prompt = st.chat_input("Ask: what is blocked, who owns the next action, or what changed?")
    if prompt:
        st.session_state.chat_history.append({"role": "user", "content": prompt, "sources": []})
        answer, sources = answer_from_evidence(prompt)
        st.session_state.chat_history.append({"role": "assistant", "content": answer, "sources": sources})
        record_event("Asked evidence copilot", "chat", f"Question answered from {len(sources)} authorised source(s).")
        st.rerun()
    if st.session_state.chat_history and st.button("Clear chat history", key="clear_chat"):
        st.session_state.chat_history = []
        record_event("Cleared chat history", "chat", "User cleared the in-session copilot conversation.")
        st.rerun()


def render_security() -> None:
    st.markdown("<div class='section-kicker'>BOUNDARIES MATTER</div><h2 class='section-title'>Security, access & audit</h2><p class='muted'>Useful knowledge is not the same as all knowledge. This is where the product makes the boundary visible.</p>", unsafe_allow_html=True)
    cols = st.columns(4)
    cols[0].metric("Allowed groups", len(st.session_state.allowed_sensitivity))
    cols[1].metric("Visible sources", len(visible_evidence()))
    cols[2].metric("Hidden sources", len(st.session_state.evidence) - len(visible_evidence()))
    cols[3].metric("Audit events", len(st.session_state.audit_log))
    left, right = st.columns([1.05, 1])
    with left:
        st.markdown("### Current publication policy")
        st.json({"recipient": st.session_state.recipient_role, "recipient_description": RECIPIENTS[st.session_state.recipient_role]["description"], "allowed_sensitivity": st.session_state.allowed_sensitivity, "publication_rule": "Approved claims only", "expiry": st.session_state.share_expiry})
        st.markdown("### Redaction preview")
        hidden = [item for item in st.session_state.evidence if item.sensitivity not in st.session_state.allowed_sensitivity]
        if hidden:
            for item in hidden[:5]:
                st.markdown(f"<div style='padding:.55rem;border-radius:10px;background:#f4e9f4;color:#704c73;margin:.35rem 0'><b>{item.evidence_id}</b> · {item.source_name} · hidden from this audience</div>", unsafe_allow_html=True)
        else:
            st.success("No restricted sources are hidden for this audience.")
    with right:
        st.markdown("### Approval history")
        if not st.session_state.audit_log:
            st.caption("No events yet.")
        for event in st.session_state.audit_log[:10]:
            st.markdown(f"**{event.action}** · {event.target}\n\n<span class='muted'>{event.timestamp} · {event.detail}</span>", unsafe_allow_html=True)


def render_architecture() -> None:
    st.markdown("<div class='section-kicker'>BUILD BLUEPRINT</div><h2 class='section-title'>From hackathon demo to production system</h2><p class='muted'>The vertical slice below mirrors the proposed technology stack and makes the responsible-AI boundary explicit.</p>", unsafe_allow_html=True)
    layers = [
        ("1 · Authorised ingestion", "OAuth 2.0 / OIDC connectors", "GitHub · Drive / Docs · Chat exports · task boards\nPermission metadata preserved at retrieval."),
        ("2 · Evidence normalisation", "FastAPI workers + PostgreSQL", "Source IDs · timestamps · authors · locators · freshness\nDeduplication and incremental sync."),
        ("3 · Retrieval & intelligence", "pgvector + structured LLM output", "Evidence-grounded extraction into a project-state graph\nDecisions · questions · risks · owners · dependencies · actions."),
        ("4 · Human control", "Streamlit review workspace", "Claim-level citations · confidence · conflict flags\nEdit · approve · reject · request evidence."),
        ("5 · Safe publication", "RBAC / ABAC + audit log", "Recipient-scoped views · redaction preview · expiry\nApproved Markdown / web handover only."),
    ]
    for title, tech, detail in layers:
        st.markdown(f"<div class='architecture'><h4>{title}</h4><p><b>{tech}</b><br>{detail.replace(chr(10), '<br>')}</p></div>", unsafe_allow_html=True)
        if title != layers[-1][0]: st.markdown("<div class='arrow'>↓</div>", unsafe_allow_html=True)
    st.markdown("### Product impact instrumentation")
    impact = st.columns(4)
    impact[0].metric("Transition time", "Target −50%")
    impact[1].metric("Citation coverage", "Target ≥80%")
    impact[2].metric("Access leaks", "Target 0")
    impact[3].metric("First contribution", "Track pilot")
    st.caption("In production, persist these metrics and compare manual handovers against Handover Intelligence in a controlled pilot.")


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="✦", layout="wide", initial_sidebar_state="expanded")
    init_state()
    inject_theme()
    render_sidebar()
    st.markdown("<section class='hero'><div class='eyebrow'>PROJECT CONTINUITY STUDIO · HUMAN-CONTROLLED AI</div><h1>Make the invisible work visible.</h1><p>Reconstruct the current state of a project from authorised evidence — then hand it over with sources, uncertainty, and next actions still attached.</p></section>", unsafe_allow_html=True)
    tabs = st.tabs(["Command center", "Evidence vault", "Review queue", "Handover pack", "Evidence copilot", "Security & audit", "Architecture"])
    with tabs[0]: render_overview()
    with tabs[1]: render_evidence()
    with tabs[2]: render_review()
    with tabs[3]: render_handover()
    with tabs[4]: render_copilot()
    with tabs[5]: render_security()
    with tabs[6]: render_architecture()


if __name__ == "__main__":
    main()
