"""Offline checks for the Plane C hybrid-ranking contract.

The blend itself runs in SQL, so these tests pin the structural invariants that
keep the vector path a *ranker* and never a *gate*: the weights sum to 1, the
trigram-only query never references the embedding column, and the hybrid query
blends both signals.
"""

from governor.memory import planner


def test_blend_weights_sum_to_one():
    assert planner.VECTOR_WEIGHT + planner.TRIGRAM_WEIGHT == 1.0


def test_trigram_only_sql_does_not_touch_embeddings():
    # With the flag off (or no embedding), Plane C must run pure trigram — it
    # must not reference the vector column, so a fork without pgvector still works.
    assert "embedding" not in planner._TRIGRAM_SQL
    assert "similarity(content" in planner._TRIGRAM_SQL


def test_hybrid_sql_blends_vector_and_trigram():
    sql = planner._HYBRID_SQL
    assert "embedding <=>" in sql  # cosine distance against the query vector
    assert "similarity(content" in sql  # trigram text similarity
    # the candidate set is a UNION of vector hits OR trigram hits, never an
    # AND — a row that only trigram-matches must still be a candidate.
    assert " OR " in sql


def test_hybrid_keeps_not_yet_embedded_rows():
    # Rows whose embedding is still NULL (pending Honcho sync) must remain
    # eligible via the trigram branch.
    assert "embedding IS NOT NULL" in planner._HYBRID_SQL
