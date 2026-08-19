from __future__ import annotations

from brunner import (
    CampaignPlan,
    CampaignTrial,
    ClusterCampaign,
    ControllerProfile,
)
from brunner.backends import KubernetesProfile

from examples.diffusion_benchmark.definition import AZURE_OPENAI_BASE_URL
from examples.diffusion_benchmark.images import (
    AGENT_IMAGE,
    CONTROLLER_IMAGE,
    SQUID_IMAGE,
)


def build_campaign(definition, contract) -> ClusterCampaign:
    del definition, contract
    plan = CampaignPlan(
        campaign_id="diffusion-equation-example",
        trials=(
            CampaignTrial(
                "luna-low",
                "codex",
                "gpt-5.6-luna",
                effort="low",
                provider_id="azure",
                provider_name="RENCI Azure OpenAI",
                base_url=AZURE_OPENAI_BASE_URL,
                environment_key="AZURE_OPENAI_API_KEY",
            ),
            CampaignTrial(
                "sonnet-low",
                "claude",
                "claude-sonnet-5",
                effort="low",
            ),
        ),
        max_parallel=2,
        backend_image=AGENT_IMAGE,
        cpu_request="1",
        cpu_limit="4",
        memory_request="2Gi",
        memory_limit="8Gi",
        ephemeral_storage_request="512Mi",
        ephemeral_storage_limit="2Gi",
        provider_secret_environment={
            "codex": {
                "AZURE_OPENAI_API_KEY": (
                    "codex-provider-credentials",
                    "AZURE_OPENAI_API_KEY",
                )
            },
            "claude": {
                "CLAUDE_CODE_OAUTH_TOKEN": (
                    "claude-provider-credentials",
                    "CLAUDE_CODE_OAUTH_TOKEN",
                )
            },
        },
        submission_retry_seconds=15,
        collection_retry_seconds=15,
        cleanup_retry_seconds=15,
        publication_retry_seconds=15,
        trial_timeout_seconds=25 * 60,
        trial_timeout_margin_seconds=2 * 60,
        evaluation_timeout_seconds=3 * 60,
    )
    return ClusterCampaign(
        plan=plan,
        backend=KubernetesProfile(
            namespace="bizon",
            network_isolation_mode="controlled-egress",
            agent_image=AGENT_IMAGE,
            artifact_reader_image=CONTROLLER_IMAGE,
            storage_size="2Gi",
            storage_class_name="basic",
            image_pull_secrets=("registry-credentials",),
            proxy_image=SQUID_IMAGE,
            max_parallel=2,
            command_timeout_seconds=60,
            staging_timeout_seconds=3 * 60,
            reader_timeout_seconds=3 * 60,
        ),
        controller=ControllerProfile(
            namespace="bizon",
            image=CONTROLLER_IMAGE,
            control_storage_size="5Gi",
            results_storage_size="5Gi",
            storage_class_name="basic",
            image_pull_secrets=("registry-credentials",),
            poll_seconds=2,
            preparation_timeout_seconds=5 * 60,
            command_timeout_seconds=60,
            max_published_trial_bytes=100 * 1024 * 1024,
            controller_cpu_request="250m",
            controller_cpu_limit="1",
            controller_memory_request="512Mi",
            controller_memory_limit="2Gi",
            assessment_cpu_request="500m",
            assessment_cpu_limit="2",
            assessment_memory_request="1Gi",
            assessment_memory_limit="4Gi",
            reviewer_secret_environment={
                "codex": {
                    "AZURE_OPENAI_API_KEY": (
                        "codex-provider-credentials",
                        "AZURE_OPENAI_API_KEY",
                    )
                }
            },
        ),
    )
