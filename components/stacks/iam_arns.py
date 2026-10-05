"""Config-driven Bedrock InvokeModel resource-ARN builder.

Converts the ``models`` map and ``inference_profile_fanout_regions`` from
config.yml into the exact list of resource ARNs the data-plane managed policy
must allow ``bedrock:InvokeModel`` on.

LOCKED FIX 5: inference_profile_fanout_regions {us-east-1, us-east-2,
us-west-2} is a VERIFIED SNAPSHOT of the regions the ``us.`` inference
profiles fan out to today. It MUST be re-checked via the Bedrock
``GetInferenceProfile`` API whenever a profile id changes, because invoking a
cross-region inference profile also requires permission on the backing
foundation-model ARN in every region the profile may route to.
"""

from __future__ import annotations

from typing import Dict, List

_US_PREFIX = "us."


def build_invoke_model_resource_arns(
    models: Dict[str, str],
    fanout_regions: List[str],
    account: str,
) -> List[str]:
    """Build the InvokeModel resource ARNs for every configured model id.

    Rules:
      * A ``us.``-prefixed id is a cross-region inference profile. It emits,
        for EVERY region in ``fanout_regions``:
          - the inference-profile ARN
            ``arn:aws:bedrock:<region>:<account>:inference-profile/<us.id>``
          - the backing foundation-model ARN (leading ``us.`` stripped)
            ``arn:aws:bedrock:<region>::foundation-model/<id>``
      * A bare id is an on-demand foundation model. It emits exactly one
        region-less foundation-model ARN
        ``arn:aws:bedrock:::foundation-model/<id>``. The id itself does not
        carry its home region (that lives in config.yml alongside
        embedding_region/image_region), so a region-neutral foundation-model
        ARN is used; it resolves in whichever single region the on-demand
        model is invoked from without over-granting across the fan-out set.

    The returned list is de-duplicated while preserving insertion order.
    """
    arns: List[str] = []

    for model_id in models.values():
        if not model_id:
            continue
        if model_id.startswith(_US_PREFIX):
            backing_id = model_id[len(_US_PREFIX):]
            for region in fanout_regions:
                arns.append(
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_id}"
                )
                arns.append(
                    f"arn:aws:bedrock:{region}::foundation-model/{backing_id}"
                )
        else:
            # Bare (on-demand) id: single region-less foundation-model ARN.
            arns.append(f"arn:aws:bedrock:::foundation-model/{model_id}")

    # De-duplicate, preserve order.
    seen = set()
    deduped: List[str] = []
    for arn in arns:
        if arn not in seen:
            seen.add(arn)
            deduped.append(arn)
    return deduped
