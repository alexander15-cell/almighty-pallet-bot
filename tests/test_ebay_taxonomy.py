"""
ebay_taxonomy.py - the searchable index built from eBay's own official
category export (ebay_categories.json). This is what replaced the old
hand-typed EBAY_CATEGORIES list in config.py, which had repeatedly caused
real upload failures (non-leaf IDs, and once two categories sharing the
same ID, which crashed the whole approval flow). These tests run against
the real bundled data file, not a fixture, since the whole point is that
every ID it returns is real.
"""
import ebay_taxonomy


def test_data_file_loaded_with_many_leaf_categories():
    # Sanity check the real bundled data, not a fixture - eBay's June 2026
    # export has ~18,000 leaf categories.
    assert len(ebay_taxonomy._INDEX) > 10000


def test_every_category_id_is_unique():
    ids = [category_id for category_id, _, _, _ in ebay_taxonomy._INDEX]
    assert len(ids) == len(set(ids))


def test_get_path_resolves_a_known_id():
    # 176937 is Ceiling Fans under Home & Garden - stable in eBay's taxonomy.
    path = ebay_taxonomy.get_path("176937")
    assert path is not None
    assert "Ceiling Fans" in path


def test_get_path_returns_none_for_unknown_id():
    assert ebay_taxonomy.get_path("not-a-real-id") is None


def test_search_ranks_exact_leaf_match_first():
    results = ebay_taxonomy.search("ceiling fan", limit=5)
    assert results, "expected at least one match"
    top_id, top_path = results[0]
    assert top_id == "176937"
    assert top_path.endswith("Ceiling Fans")


def test_search_prefers_leaf_match_over_ancestor_only_match():
    # A query matching a category's own leaf name should always outrank one
    # that only matches somewhere in its ancestor breadcrumb.
    results = ebay_taxonomy.search("impact wrench", limit=5)
    assert results
    top_id, top_path = results[0]
    assert "Impact Wrench" in top_path.rsplit(" > ", 1)[-1]


def test_search_handles_simple_plural_mismatch():
    # "wrench" (singular) should still find a leaf named "Impact Wrenches"
    # (plural) - the crude trailing-s stemming in _tokenize.
    results = ebay_taxonomy.search("wrench", limit=10)
    assert any("Wrench" in path for _, path in results)


def test_search_returns_empty_for_blank_query():
    assert ebay_taxonomy.search("", limit=5) == []
    assert ebay_taxonomy.search("   ", limit=5) == []


def test_search_returns_empty_for_nonsense_query():
    assert ebay_taxonomy.search("zzxxqqwwjjkk999", limit=5) == []


def test_search_respects_limit():
    results = ebay_taxonomy.search("light", limit=3)
    assert len(results) <= 3


def test_search_results_have_unique_ids():
    results = ebay_taxonomy.search("light", limit=25)
    ids = [category_id for category_id, _ in results]
    assert len(ids) == len(set(ids))
