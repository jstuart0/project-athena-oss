"""Service run-state derivation (ATHENA-118, D2).

Single source of truth for turning a registry row's raw column state
(``enabled``, ``health_status``, optionally a Kubernetes replica count) into
the run-state vocabulary the Service Control page and its API consumers
share: ``running`` | ``stopped`` | ``disabled``.

The poller's health vocabulary is ``healthy`` | ``unconfigured`` |
``unhealthy`` (verified against ``health_poller.py``); any other string
(``None``, a stale/unknown value) is treated as not-healthy so an
unrecognized status never fails open into ``running``.
"""
from typing import Optional


def derive_run_state(
    enabled: bool,
    health_status: Optional[str],
    k8s_replicas: Optional[int] = None,
) -> str:
    """Derive the manager-agnostic run state for a registry row (D2).

    | enabled | health_status          | k8s_replicas | run_state  |
    |---------|-------------------------|--------------|------------|
    | False   | any                     | any          | disabled   |
    | True    | any                     | 0            | stopped    |
    | True    | healthy / unconfigured  | None / >=1   | running    |
    | True    | anything else (incl None)| None / >=1  | stopped    |
    """
    if not enabled:
        return 'disabled'
    if k8s_replicas == 0:
        return 'stopped'
    if health_status in ('healthy', 'unconfigured'):
        return 'running'
    return 'stopped'


def normalized_health_status(enabled: bool, health_status: Optional[str]) -> str:
    """Normalize a row's raw ``health_status`` for display (shared by
    service_registry.py and the service-control envelope, D3).

    A disabled row always reports ``'disabled'`` regardless of whatever
    value the poller cached before it stopped being polled (ATHENA-112).
    A never-polled enabled row reports ``'pending'`` rather than a raw
    ``None`` so the UI doesn't have to special-case null.
    """
    if not enabled:
        return 'disabled'
    if health_status is None:
        return 'pending'
    return health_status
