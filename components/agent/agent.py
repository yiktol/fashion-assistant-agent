"""In-process Strands agent runtime: client factory + ``build_agent``.

Builds the two-region boto3 client split, wires the five ``@tool`` functions over
their injected clients/settings, and constructs the Strands ``Agent`` with the
Claude Sonnet 4.5 ``BedrockModel`` brain.

LOCKED FIX 2: there is NO lazy ``bootstrap_index`` path. ``build_agent`` calls
``s3vectors.get_index`` and raises a fatal :class:`ConfigError` on NotFound,
telling the operator to run the ingest notebook once to create and populate the
index. The ingest notebook is the only index creator/populator.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError
from strands import Agent
from strands.models import BedrockModel

from .prompt import system_prompt
from .settings import ConfigError, Settings, load_settings
from .tools.image_gen import make_generate_image, make_inpaint, make_outpaint
from .tools.image_lookup import make_image_lookup
from .tools.weather import get_weather

logger = logging.getLogger(__name__)


class Clients:
    """Bundle of the boto3 clients the tools need, split by region."""

    def __init__(self, settings: Settings) -> None:
        self.bedrock_primary = boto3.client(
            "bedrock-runtime", region_name=settings.primary_region
        )
        self.bedrock_embedding = boto3.client(
            "bedrock-runtime", region_name=settings.embedding_region
        )
        self.bedrock_image = boto3.client(
            "bedrock-runtime", region_name=settings.image_region
        )
        self.s3 = boto3.client("s3", region_name=settings.primary_region)
        self.s3vectors = boto3.client("s3vectors", region_name=settings.primary_region)


@lru_cache(maxsize=None)
def _build_clients(settings: Settings) -> Clients:
    """Cached client factory keyed by the (frozen, hashable) settings."""
    return Clients(settings)


def _assert_index_exists(s3vectors, settings: Settings) -> None:
    """Fatal check that the vector index exists (LOCKED FIX 2).

    Raises:
        ConfigError: when the index is absent -- the operator must run the ingest
            notebook once to create and populate it.
    """
    try:
        s3vectors.get_index(
            vectorBucketName=settings.vector_bucket, indexName=settings.vector_index
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NotFoundException", "ResourceNotFoundException", "NotFound"):
            raise ConfigError(
                f"vector index '{settings.vector_index}' not found in bucket "
                f"'{settings.vector_bucket}'; run the ingest notebook once to "
                "create and populate the index"
            ) from exc
        raise


def build_agent(settings: Settings | None = None, callback_handler=None, hooks=None):
    """Construct the Strands fashion agent.

    Args:
        settings: resolved :class:`Settings`; loaded from disk when omitted.
        callback_handler: optional trace-text callback (streamed reasoning +
            tool-call announcements only, never result extraction).
        hooks: optional list of ``HookProvider`` (e.g. ``ToolResultCollector``).

    Raises:
        ConfigError: when the vector index does not yet exist (LOCKED FIX 2).
    """
    if settings is None:
        settings = load_settings()

    clients = _build_clients(settings)

    # LOCKED FIX 2: fatal GetIndex check -- no lazy bootstrap anywhere.
    _assert_index_exists(clients.s3vectors, settings)

    tools = [
        get_weather,
        make_generate_image(clients.bedrock_image, clients.s3, settings),
        make_image_lookup(
            clients.bedrock_embedding, clients.s3vectors, clients.s3, settings
        ),
        make_inpaint(clients.bedrock_primary, clients.s3, settings),
        make_outpaint(clients.bedrock_primary, clients.s3, settings),
    ]

    model = BedrockModel(
        region_name=settings.primary_region, model_id=settings.agent_model_id
    )

    return Agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        callback_handler=callback_handler,
        hooks=hooks or [],
    )
