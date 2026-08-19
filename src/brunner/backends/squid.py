from __future__ import annotations

import hashlib
import ipaddress
from typing import Any


MANAGED_PROXY_NAME = "brunner-egress-proxy"
MANAGED_PROXY_PORT = 3128
MANAGED_PROXY_LABELS = {
    "app.kubernetes.io/name": MANAGED_PROXY_NAME,
    "app.kubernetes.io/part-of": "brunner",
}
PROVIDER_DOMAINS = (
    ".openai.com",
    ".openai.azure.com",
    ".anthropic.com",
    ".claude.ai",
)
SQUID_CONFIG = (
    "http_port 3128\n"
    "\n"
    "acl SSL_ports port 443\n"
    "acl CONNECT method CONNECT\n"
    f"acl provider_domains dstdomain -n {' '.join(PROVIDER_DOMAINS)}\n"
    "\n"
    "http_access deny !CONNECT\n"
    "http_access deny !SSL_ports\n"
    "http_access allow CONNECT provider_domains\n"
    "http_access deny all\n"
    "\n"
    "access_log stdio:/var/log/squid/access.log\n"
    "cache deny all\n"
)
SQUID_CONFIG_SHA256 = hashlib.sha256(SQUID_CONFIG.encode()).hexdigest()


def managed_proxy_sha256(image: str) -> str:
    payload = f"{image}\0{SQUID_CONFIG_SHA256}".encode()
    return hashlib.sha256(payload).hexdigest()


def managed_proxy_labels(
    name: str,
    *,
    campaign_labels: dict[str, str] | None = None,
) -> dict[str, str]:
    labels = {
        **MANAGED_PROXY_LABELS,
        "app.kubernetes.io/instance": name,
    }
    if campaign_labels:
        labels.update(campaign_labels)
    return labels


def render_managed_proxy_resources(
    *,
    name: str,
    namespace: str,
    image: str,
    image_pull_secrets: tuple[str, ...],
    dns_namespace: str,
    dns_pod_selector: dict[str, str],
    cpu_request: str,
    cpu_limit: str,
    memory_request: str,
    memory_limit: str,
    campaign_labels: dict[str, str] | None = None,
) -> tuple[dict[str, Any], ...]:
    labels = managed_proxy_labels(
        name,
        campaign_labels=campaign_labels,
    )
    selector_labels = managed_proxy_labels(name)
    workload_source_labels = {
        "app.kubernetes.io/name": "brunner",
        **(campaign_labels or {}),
    }
    pod_spec: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 13,
            "runAsGroup": 13,
            "fsGroup": 13,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "squid",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "ports": [
                    {
                        "name": "proxy",
                        "containerPort": MANAGED_PROXY_PORT,
                        "protocol": "TCP",
                    }
                ],
                "resources": {
                    "requests": {
                        "cpu": cpu_request,
                        "memory": memory_request,
                    },
                    "limits": {
                        "cpu": cpu_limit,
                        "memory": memory_limit,
                    },
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "readOnlyRootFilesystem": True,
                },
                "volumeMounts": [
                    {
                        "name": "config",
                        "mountPath": "/etc/squid/squid.conf",
                        "subPath": "squid.conf",
                        "readOnly": True,
                    },
                    {"name": "log", "mountPath": "/var/log/squid"},
                    {"name": "run", "mountPath": "/run"},
                    {"name": "spool", "mountPath": "/var/spool/squid"},
                ],
            }
        ],
        "volumes": [
            {
                "name": "config",
                "configMap": {"name": name},
            },
            {"name": "log", "emptyDir": {}},
            {"name": "run", "emptyDir": {}},
            {"name": "spool", "emptyDir": {}},
        ],
    }
    if image_pull_secrets:
        pod_spec["imagePullSecrets"] = [
            {"name": name} for name in image_pull_secrets
        ]
    return (
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": dict(labels),
            },
            "data": {"squid.conf": SQUID_CONFIG},
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": dict(labels),
            },
            "spec": {
                "selector": dict(selector_labels),
                "ports": [
                    {
                        "name": "proxy",
                        "port": MANAGED_PROXY_PORT,
                        "targetPort": "proxy",
                        "protocol": "TCP",
                    }
                ],
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": dict(labels),
            },
            "spec": {
                "podSelector": {
                    "matchLabels": dict(selector_labels),
                },
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [
                    {
                        "from": [
                            {
                                "podSelector": {
                                    "matchLabels": {
                                        **workload_source_labels,
                                        "dev.brunner/role": "pipeline",
                                    }
                                }
                            },
                            {
                                "podSelector": {
                                    "matchLabels": {
                                        **workload_source_labels,
                                        "dev.brunner/role": "assessment",
                                    }
                                }
                            },
                        ],
                        "ports": [
                            {
                                "protocol": "TCP",
                                "port": MANAGED_PROXY_PORT,
                            }
                        ],
                    }
                ],
                "egress": [
                    {
                        "to": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {
                                        "kubernetes.io/metadata.name": (
                                            dns_namespace
                                        )
                                    }
                                },
                                "podSelector": {
                                    "matchLabels": dict(
                                        dns_pod_selector
                                    )
                                },
                            }
                        ],
                        "ports": [
                            {"protocol": "UDP", "port": 53},
                            {"protocol": "TCP", "port": 53},
                        ],
                    },
                    {
                        "ports": [
                            {"protocol": "TCP", "port": 443},
                        ]
                    },
                ],
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": dict(labels),
            },
            "spec": {
                "replicas": 1,
                "selector": {
                    "matchLabels": dict(selector_labels),
                },
                "template": {
                    "metadata": {
                        "labels": dict(selector_labels),
                        "annotations": {
                            "dev.brunner/squid-config-sha256": (
                                SQUID_CONFIG_SHA256
                            )
                        },
                    },
                    "spec": pod_spec,
                },
            },
        },
    )


def proxy_url_from_service(service: dict[str, Any]) -> str:
    spec = service.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("managed proxy Service has no spec")
    cluster_ip = spec.get("clusterIP")
    if not isinstance(cluster_ip, str) or not cluster_ip.strip():
        raise ValueError("managed proxy Service has no ClusterIP")
    if cluster_ip.lower() == "none":
        raise ValueError("managed proxy Service cannot be headless")
    try:
        address = ipaddress.ip_address(cluster_ip)
    except ValueError as error:
        raise ValueError(
            f"managed proxy Service has invalid ClusterIP {cluster_ip!r}"
        ) from error
    ports = spec.get("ports")
    if not isinstance(ports, list) or not any(
        isinstance(port, dict)
        and port.get("port") == MANAGED_PROXY_PORT
        and port.get("protocol", "TCP") == "TCP"
        for port in ports
    ):
        raise ValueError(
            f"managed proxy Service does not expose TCP {MANAGED_PROXY_PORT}"
        )
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{host}:{MANAGED_PROXY_PORT}"
