# Handover Intelligence

A Python + Streamlit MVP that turns scattered project activity into a reviewable, evidence-linked handover.

## What this MVP does

- Ingests TXT, Markdown, CSV, JSON, PDF, and DOCX evidence.
- Includes a sample project so the workflow can be tested immediately.
- Extracts decisions, open questions, risks, responsibilities, dependencies, and next actions.
- Keeps every generated claim linked to evidence IDs and source metadata.
- Supports confidence labels and explicit statuses: Draft, Approved, Needs evidence, Conflicting, and Rejected.
- Applies a simple sensitivity filter before analysis and export.
- Requires human approval before claims appear in the approved handover.
- Exports an approved Markdown handover and an audit JSON file.
- Optionally uses an OpenAI-compatible model for structured extraction when `OPENAI_API_KEY` is configured. The app remains usable with deterministic extraction when no key is present.

## Quick start

```bash
git clone <your-repository-url>
cd handover-intelligence
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
streamlit run app.py
```

Open the local URL printed by Streamlit, then click **Load sample project** in the sidebar.

## Optional AI extraction

The default MVP uses transparent deterministic rules, which makes it easy to demo and test. For model-assisted extraction, set these environment variables before starting Streamlit:

```bash
export OPENAI_API_KEY="your-key"
# Optional:
export OPENAI_MODEL="gpt-4o-mini"
# Optional for an OpenAI-compatible provider:
export OPENAI_API_BASE="https://your-provider.example/v1"
```

Then turn on **Use optional OpenAI extraction** in the sidebar. The app asks the model for JSON claims, and still requires citations and human approval.

For Streamlit Community Cloud, add the key through **App settings → Secrets**:

```toml
OPENAI_API_KEY = "your-key"
OPENAI_MODEL = "gpt-4o-mini"
```

Do not commit `.streamlit/secrets.toml` or `.env`.

## Deploy on Streamlit Community Cloud

1. Create a GitHub repository and push `app.py`, `requirements.txt`, `README.md`, and `.gitignore`.
2. In Streamlit Community Cloud, choose **Create app**.
3. Select the repository, branch, and `app.py` as the main file.
4. Deploy.
5. Add `OPENAI_API_KEY` only if you want model-assisted extraction.

## MVP architecture

```text
Uploads / sample evidence
          |
          v
  Evidence normalisation  ---> source IDs, locators, sensitivity
          |
          v
  Deterministic extractor  ---> optional structured LLM extractor
          |
          v
  Claim review queue       ---> edit, cite, approve, reject, flag uncertainty
          |
          v
  Audience filter + export ---> approved-handover.md + handover-audit.json
```

The current app keeps state in `st.session_state`, which is appropriate for a demo but not for multi-user production use.

## Production upgrade path

1. Replace file uploads with OAuth connectors for GitHub, Drive, Slack/Chat, Notion, and task boards.
2. Persist evidence, claims, reviews, and audit events in PostgreSQL.
3. Use a background job queue for incremental sync and retries.
4. Re-check source permissions at retrieval time and publication time; add row-level access policies.
5. Store embeddings in pgvector for evidence-grounded retrieval.
6. Add redaction, retention, deletion, legal hold, and data residency controls.
7. Add authentication and recipient-specific handover links.
8. Build evaluation datasets for citation accuracy, extraction precision, conflict detection, prompt injection, and permission boundaries.

## Important security note

This repository is a prototype. It does not yet provide enterprise authentication, durable audit storage, source OAuth permission enforcement, encryption configuration, or complete redaction. Treat uploaded data as sensitive and use synthetic/sample data for demos until those controls are implemented.

## Suggested demo flow

1. Load the sample project.
2. Go to **Evidence** and inspect source IDs and restricted evidence.
3. Click **Analyse project**.
4. Go to **Review**, edit one claim, mark a risk as **Needs evidence**, and approve the supported items.
5. Open **Handover** to see that only approved claims are shared.
6. Open **Access** to show the sensitivity filter and publication rule.
