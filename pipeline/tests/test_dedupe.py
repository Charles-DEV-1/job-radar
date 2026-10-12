from copy import deepcopy
from datetime import UTC, datetime

from test_filters import example_job

from pipeline.dedupe import hash_job


def test_identity_normalizes_case_punctuation_and_whitespace() -> None:
    first = hash_job(example_job())
    second = example_job(title="  BACKEND  Engineer", location="Lagos - Nigeria")
    second.company = "EXAMPLE  COMPANY"
    assert hash_job(second).canonical_key == first.canonical_key


def test_content_hash_ignores_ids_urls_seen_time_and_filter_result() -> None:
    first = hash_job(example_job())
    second = deepcopy(first)
    second.source = "lever"
    second.external_id = "different-provider-id"
    second.apply_url = "https://example.org/apply"
    second.first_seen_at = datetime(2030, 1, 1, tzinfo=UTC)
    second.filter_result = "exclude"
    assert hash_job(second).content_hash == first.content_hash
    second.description_text = "Changed material content"
    assert hash_job(second).content_hash != first.content_hash
    assert second.canonical_key == first.canonical_key
