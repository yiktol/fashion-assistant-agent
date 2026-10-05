"""Typed configuration for the fashion assistant agent.

Merges ``config.yml`` defaults with CDK deploy outputs (``variables.json``) and
a small set of environment overrides, then validates the result into a frozen
:class:`Settings`. This module never creates a vector index and never calls
``GetIndex`` -- the fatal "index must exist" check lives in ``build_agent``
(FEAT-003, Locked Fix 2). No AWS profile names appear here (NFR-1); boto3's
default credential chain is used downstream.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Repository root: components/agent/settings.py -> <root>
_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _ROOT / "config.yml"
_DEFAULT_VARIABLES_PATH = _ROOT / "variables.json"

# Environment override names.
_ENV_PRIMARY_REGION = "FASHION_PRIMARY_REGION"
_ENV_IMAGE_REGION = "FASHION_IMAGE_REGION"
_ENV_EMBEDDING_REGION = "FASHION_EMBEDDING_REGION"

# variables.json keys produced by `cdk deploy --outputs-file variables.json`.
_VAR_BUCKET = "BucketName"
_VAR_VECTOR_BUCKET = "VectorBucketName"
_VAR_VECTOR_INDEX = "VectorIndexName"

# Known model-availability map. Values are the regions where each on-demand
# model exists today; re-check when AWS expands regional coverage.
#   Nova 2 multimodal embeddings -> us-east-1 only
#   Stability stable-image-core / stable-image-ultra -> us-west-2 only
_EMBEDDING_REGIONS = frozenset({"us-east-1"})
_IMAGE_REGIONS = frozenset({"us-west-2"})

_REQUIRED_MODEL_KEYS = (
    "agent",
    "embeddings",
    "text_to_image",
    "text_to_image_alt",
    "inpaint",
    "outpaint",
    "search_replace",
    "search_recolor",
)


class ConfigError(Exception):
    """Raised when configuration is missing, incomplete, or inconsistent."""


@dataclass(frozen=True)
class Settings:
    """Resolved, validated runtime configuration."""

    primary_region: str
    image_region: str
    embedding_region: str

    agent_model_id: str
    embedding_model_id: str
    text_to_image_model_id: str
    text_to_image_alt_model_id: str
    inpaint_model_id: str
    outpaint_model_id: str
    search_replace_model_id: str
    search_recolor_model_id: str

    inference_profile_fanout_regions: tuple[str, ...]
    expected_embedding_dimension: int | None

    image_bucket: str
    vector_bucket: str
    vector_index: str
    top_k: int
    distance_metric: str


def _load_yaml(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config file is not a mapping: {config_path}")
    return data


def _load_variables(variables_path: Path) -> dict[str, Any]:
    if not variables_path.exists():
        raise ConfigError(
            f"variables.json not found at {variables_path}; "
            "run `cdk deploy --outputs-file variables.json` first"
        )
    with variables_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ConfigError(f"variables.json is not a mapping: {variables_path}")
    return data


def _flatten_outputs(raw: dict[str, Any]) -> dict[str, Any]:
    """Return CDK outputs regardless of the single-stack wrapper shape.

    ``cdk deploy --outputs-file`` nests outputs under the stack name, e.g.
    ``{"FashionAgentStack": {"BucketName": "..."}}``. Accept either the nested
    form or an already-flat mapping of output keys.
    """
    if _VAR_BUCKET in raw or _VAR_VECTOR_BUCKET in raw or _VAR_VECTOR_INDEX in raw:
        return raw
    # Merge the values of nested stack mappings.
    flat: dict[str, Any] = {}
    for value in raw.values():
        if isinstance(value, dict):
            flat.update(value)
    return flat


def _require(outputs: dict[str, Any], key: str) -> str:
    value = outputs.get(key)
    if value is None or value == "":
        raise ConfigError(f"missing required variables.json key: {key}")
    return str(value)


def load_settings(
    config_path: str | os.PathLike[str] | None = None,
    variables_path: str | os.PathLike[str] | None = None,
) -> Settings:
    """Load, merge, and validate configuration into a :class:`Settings`.

    Args:
        config_path: Path to ``config.yml``. Defaults to the repo-root file.
        variables_path: Path to the CDK ``variables.json`` outputs file.
            Defaults to the repo-root file.

    Raises:
        ConfigError: when required model ids or ``variables.json`` keys are
            missing, or when ``embedding_region``/``image_region`` name a region
            where the required model is not available.
    """
    cfg_path = Path(config_path) if config_path is not None else _DEFAULT_CONFIG_PATH
    var_path = (
        Path(variables_path) if variables_path is not None else _DEFAULT_VARIABLES_PATH
    )

    config = _load_yaml(cfg_path)
    outputs = _flatten_outputs(_load_variables(var_path))

    # Regions, with environment overrides.
    primary_region = os.environ.get(_ENV_PRIMARY_REGION) or config.get("primary_region")
    image_region = os.environ.get(_ENV_IMAGE_REGION) or config.get("image_region")
    embedding_region = os.environ.get(_ENV_EMBEDDING_REGION) or config.get(
        "embedding_region"
    )
    for name, value in (
        ("primary_region", primary_region),
        ("image_region", image_region),
        ("embedding_region", embedding_region),
    ):
        if not value:
            raise ConfigError(f"missing required region: {name}")

    # Validate region availability for region-locked models.
    if embedding_region not in _EMBEDDING_REGIONS:
        raise ConfigError(
            f"embedding_region '{embedding_region}' has no Nova 2 multimodal "
            f"embeddings model; available regions: {sorted(_EMBEDDING_REGIONS)}"
        )
    if image_region not in _IMAGE_REGIONS:
        raise ConfigError(
            f"image_region '{image_region}' has no Stability stable-image "
            f"models; available regions: {sorted(_IMAGE_REGIONS)}"
        )

    # Model ids.
    models = config.get("models") or {}
    if not isinstance(models, dict):
        raise ConfigError("config 'models' section must be a mapping")
    for key in _REQUIRED_MODEL_KEYS:
        if not models.get(key):
            raise ConfigError(f"missing required model id: models.{key}")

    # Vector index block.
    vector_index = config.get("vector_index") or {}
    if not isinstance(vector_index, dict):
        raise ConfigError("config 'vector_index' section must be a mapping")

    expected_dimension = vector_index.get("expected_dimension")
    if expected_dimension is not None:
        expected_dimension = int(expected_dimension)

    fanout = config.get("inference_profile_fanout_regions") or []
    if not isinstance(fanout, (list, tuple)) or not fanout:
        raise ConfigError("inference_profile_fanout_regions must be a non-empty list")

    # Resolve data-plane names from CDK outputs.
    image_bucket = _require(outputs, _VAR_BUCKET)
    vector_bucket = _require(outputs, _VAR_VECTOR_BUCKET)
    vector_index_name = _require(outputs, _VAR_VECTOR_INDEX)

    settings = Settings(
        primary_region=primary_region,
        image_region=image_region,
        embedding_region=embedding_region,
        agent_model_id=models["agent"],
        embedding_model_id=models["embeddings"],
        text_to_image_model_id=models["text_to_image"],
        text_to_image_alt_model_id=models["text_to_image_alt"],
        inpaint_model_id=models["inpaint"],
        outpaint_model_id=models["outpaint"],
        search_replace_model_id=models["search_replace"],
        search_recolor_model_id=models["search_recolor"],
        inference_profile_fanout_regions=tuple(fanout),
        expected_embedding_dimension=expected_dimension,
        image_bucket=image_bucket,
        vector_bucket=vector_bucket,
        vector_index=vector_index_name,
        top_k=int(vector_index.get("top_k", 3)),
        distance_metric=str(vector_index.get("distance_metric", "cosine")),
    )
    logger.info(
        "loaded settings: primary_region=%s image_region=%s embedding_region=%s "
        "expected_embedding_dimension=%s",
        settings.primary_region,
        settings.image_region,
        settings.embedding_region,
        settings.expected_embedding_dimension,
    )
    return settings
