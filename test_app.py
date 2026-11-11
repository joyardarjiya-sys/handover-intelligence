from app import Evidence, heuristic_extract


def test_extracts_core_handover_categories():
    evidence = [Evidence("E1", "notes.txt", "Text", "Decision: use PostgreSQL. The launch is blocked by OAuth. Open question: who owns the rollout? Next action: Jiya will prepare the demo.")]
    categories = {claim.category for claim in heuristic_extract(evidence)}
    assert {"Decision", "Risk", "Question", "Next action"}.issubset(categories)


def test_claims_keep_evidence_ids():
    evidence = [Evidence("E2", "notes.txt", "Text", "The project depends on identity provider approval.")]
    claims = heuristic_extract(evidence)
    assert claims and claims[0].evidence_ids == ["E2"]
