from __future__ import annotations

from brunner import (
    CampaignPlan,
    CampaignTrial,
    ClusterCampaign,
    ControllerProfile,
)
from brunner.backends import KubernetesProfile


AGENT_IMAGE = (
    "registry.example/brunner-text-agent@sha256:" + "1" * 64
)
CONTROLLER_IMAGE = (
    "registry.example/brunner-text-controller@sha256:" + "2" * 64
)
SQUID_IMAGE = "ubuntu/squid@sha256:" + "3" * 64


def build_campaign(definition, contract) -> ClusterCampaign:
    del definition, contract
    plan = CampaignPlan(
        campaign_id="text-uppercase-example",
        trials=(
            CampaignTrial(
                "codex-first-pass",
                "codex",
                "MODEL_ID",
                effort="high",
            ),
        ),
        backend_image=AGENT_IMAGE,
        provider_secret_environment={
            "codex": {
                "OPENAI_API_KEY": (
                    "codex-provider-credentials",
                    "OPENAI_API_KEY",
                )
            }
        },
    )
    return ClusterCampaign(
        plan=plan,
        backend=KubernetesProfile(
            namespace="bizon",
            network_isolation_mode="controlled-egress",
            agent_image=AGENT_IMAGE,
            artifact_reader_image=CONTROLLER_IMAGE,
            storage_size="20Gi",
            storage_class_name="sterling-storage-class",
            proxy_image=SQUID_IMAGE,
        ),
        controller=ControllerProfile(
            namespace="bizon",
            image=CONTROLLER_IMAGE,
            control_storage_size="20Gi",
            results_storage_size="50Gi",
            storage_class_name="sterling-storage-class",
        ),
    )
