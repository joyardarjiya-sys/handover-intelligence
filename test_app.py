from app import Evidence, derive_gaps, detect_conflicts, heuristic_extract


def test_extracts_core_handover_categories():
    evidence = [Evidence("E1", "notes.txt", "Text", "Decision: use PostgreSQL. The launch is blocked by OAuth. Open question: who owns the rollout? Next action: Jiya will prepare the demo.")]
    categories = {claim.category for claim in heuristic_extract(evidence)}
    assert {"Decision", "Risk", "Question", "Next action"}.issubset(categories)


def test_claims_keep_evidence_ids():
    evidence = [Evidence("E2", "notes.txt", "Text", "The project depends on identity provider approval.")]
    claims = heuristic_extract(evidence)
    assert claims and claims[0].evidence_ids == ["E2"]


def test_conflicts_and_gaps_are_explicit():
    evidence = [
        Evidence("E3", "old.txt", "Text", "The team agreed to ship with manual approval."),
        Evidence("E4", "new.txt", "Text", "Later update: automated publishing is now approved, replacing the earlier decision."),
        Evidence("E5", "retro.txt", "Text", "Unresolved question: who will monitor stale claims? No owner or due date has been recorded."),
    ]
    claims = heuristic_extract(evidence)
    detect_conflicts(claims, evidence)
    assert any(claim.conflict_flag for claim in claims)
    assert derive_gaps(claims)