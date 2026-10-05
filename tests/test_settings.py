"""Unit tests for components.agent.settings.load_settings.

All inputs are local fixtures written to tmp_path; no AWS or network calls.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from components.agent.settings import ConfigError, Settings, load_settings

_CONFIG = textwrap.dedent(
    """
    stack_name: "FashionAgentStack"
    primary_region: "us-east-1"
    image_region: "us-west-2"
    embedding_region: "us-east-1"
    bucket_name: ""
    models:
      agent: "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
      embeddings: "amazon.nova-2-multimodal-embeddings-v1:0"
      text_to_image: "stability.stable-image-core-v1:1"
      text_to_image_alt: "stability.stable-image-ultra-v1:1"
      inpaint: "us.stability.stable-image-inpaint-v1:0"
      outpaint: "us.stability.stable-outpaint-v1:0"
      search_replace: "us.stability.stable-image-search-replace-v1:0"
      search_recolor: "us.stability.stable-image-search-recolor-v1:0"
    inference_profile_fanout_regions:
      - "us-east-1"
      - "us-east-2"
      - "us-west-2"
    vector_index:
      bucket_name: "fashion-agent-vectors"
      index_name: "images-index"
      expected_dimension: null
      distance_metric: "cosine"
      top_k: 3
    """
)

_VARIABLES = {
    "BucketName": "fashion-agent-123456789012-us-east-1",
    "VectorBucketName": "fashion-agent-vectors",
    "VectorIndexName": "images-index",
}


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(_CONFIG, encoding="utf-8")
    return path


@pytest.fixture()
def variables_file(tmp_path: Path) -> Path:
    path = tmp_path / "variables.json"
    path.write_text(json.dumps(_VARIABLES), encoding="utf-8")
    return path


def test_parses_fixture(config_file: Path, variables_file: Path) -> None:
    cfg = load_settings(config_file, variables_file)
    assert isinstance(cfg, Settings)
    assert cfg.primary_region == "us-east-1"
    assert cfg.image_region == "us-west-2"
    assert cfg.embedding_region == "us-east-1"
    assert cfg.image_bucket == "fashion-agent-123456789012-us-east-1"
    assert cfg.vector_bucket == "fashion-agent-vectors"
    assert cfg.vector_index == "images-index"
    assert cfg.top_k == 3
    assert cfg.distance_metric == "cosine"
    assert cfg.inference_profile_fanout_regions == (
        "us-east-1",
        "us-east-2",
        "us-west-2",
    )


def test_expected_dimension_null_maps_to_none(
    config_file: Path, variables_file: Path
) -> None:
    cfg = load_settings(config_file, variables_file)
    assert cfg.expected_embedding_dimension is None


def test_env_overrides(
    config_file: Path, variables_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Override to still-valid regions so availability validation passes.
    monkeypatch.setenv("FASHION_PRIMARY_REGION", "us-west-2")
    monkeypatch.setenv("FASHION_IMAGE_REGION", "us-west-2")
    monkeypatch.setenv("FASHION_EMBEDDING_REGION", "us-east-1")
    cfg = load_settings(config_file, variables_file)
    assert cfg.primary_region == "us-west-2"
    assert cfg.image_region == "us-west-2"
    assert cfg.embedding_region == "us-east-1"


def test_missing_variables_key_raises(config_file: Path, tmp_path: Path) -> None:
    bad = tmp_path / "variables.json"
    partial = dict(_VARIABLES)
    del partial["VectorIndexName"]
    bad.write_text(json.dumps(partial), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(config_file, bad)


def test_impossible_embedding_region_raises(
    tmp_path: Path, variables_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(_CONFIG, encoding="utf-8")
    monkeypatch.setenv("FASHION_EMBEDDING_REGION", "eu-west-1")
    with pytest.raises(ConfigError):
        load_settings(cfg_path, variables_file)


def test_impossible_image_region_raises(
    tmp_path: Path, variables_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(_CONFIG, encoding="utf-8")
    monkeypatch.setenv("FASHION_IMAGE_REGION", "us-east-1")
    with pytest.raises(ConfigError):
        load_settings(cfg_path, variables_file)


def test_model_id_prefixes(config_file: Path, variables_file: Path) -> None:
    cfg = load_settings(config_file, variables_file)
    # us. inference-profile ids
    assert cfg.agent_model_id.startswith("us.")
    assert cfg.inpaint_model_id.startswith("us.")
    assert cfg.outpaint_model_id.startswith("us.")
    assert cfg.search_replace_model_id.startswith("us.")
    assert cfg.search_recolor_model_id.startswith("us.")
    # bare on-demand ids
    assert not cfg.embedding_model_id.startswith("us.")
    assert not cfg.text_to_image_model_id.startswith("us.")
    assert not cfg.text_to_image_alt_model_id.startswith("us.")
