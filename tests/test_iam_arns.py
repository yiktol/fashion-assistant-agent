"""Unit tests for the Bedrock InvokeModel resource-ARN builder (LOCKED FIX 5).

Pure function test: no boto3, no AWS, no network.
"""

from components.stacks.iam_arns import build_invoke_model_resource_arns

ACCOUNT = "123456789012"
FANOUT = ["us-east-1", "us-east-2", "us-west-2"]

# Mirror of config.yml models: three us.-prefixed ids and three bare ids.
MODELS = {
    "agent": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "embeddings": "amazon.nova-2-multimodal-embeddings-v1:0",
    "text_to_image": "stability.stable-image-core-v1:1",
    "text_to_image_alt": "stability.stable-image-ultra-v1:1",
    "inpaint": "us.stability.stable-image-inpaint-v1:0",
    "outpaint": "us.stability.stable-outpaint-v1:0",
    "search_replace": "us.stability.stable-image-search-replace-v1:0",
    "search_recolor": "us.stability.stable-image-search-recolor-v1:0",
}

US_PREFIXED = [
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.stability.stable-image-inpaint-v1:0",
    "us.stability.stable-outpaint-v1:0",
    "us.stability.stable-image-search-replace-v1:0",
    "us.stability.stable-image-search-recolor-v1:0",
]
BARE = [
    "amazon.nova-2-multimodal-embeddings-v1:0",
    "stability.stable-image-core-v1:1",
    "stability.stable-image-ultra-v1:1",
]


def test_us_prefixed_ids_emit_profile_and_foundation_per_fanout_region():
    arns = build_invoke_model_resource_arns(MODELS, FANOUT, ACCOUNT)
    for model_id in US_PREFIXED:
        backing = model_id[len("us."):]
        for region in FANOUT:
            profile_arn = (
                f"arn:aws:bedrock:{region}:{ACCOUNT}:inference-profile/{model_id}"
            )
            fm_arn = f"arn:aws:bedrock:{region}::foundation-model/{backing}"
            assert profile_arn in arns, (model_id, region, "missing inference-profile ARN")
            assert fm_arn in arns, (model_id, region, "missing foundation-model ARN")


def test_bare_id_emits_exactly_one_foundation_model_arn():
    arns = build_invoke_model_resource_arns(MODELS, FANOUT, ACCOUNT)
    for model_id in BARE:
        matches = [a for a in arns if a.endswith(f"foundation-model/{model_id}")]
        assert len(matches) == 1, (model_id, matches)
        assert matches[0] == f"arn:aws:bedrock:::foundation-model/{model_id}"
        # A bare id never produces an inference-profile ARN.
        assert not any(
            a.endswith(f"inference-profile/{model_id}") for a in arns
        ), model_id


def test_arns_are_deduplicated():
    arns = build_invoke_model_resource_arns(MODELS, FANOUT, ACCOUNT)
    assert len(arns) == len(set(arns))


def test_expected_total_arn_count():
    # 5 us.-prefixed * 3 regions * 2 ARNs each = 30, + 3 bare * 1 = 33.
    arns = build_invoke_model_resource_arns(MODELS, FANOUT, ACCOUNT)
    assert len(arns) == 33, arns


def test_empty_model_id_is_skipped():
    arns = build_invoke_model_resource_arns({"x": ""}, FANOUT, ACCOUNT)
    assert arns == []
