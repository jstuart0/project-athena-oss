"""Kubernetes scale-only adapter (ATHENA-118, D6/D7/D11).

A thin async adapter over `httpx` for the ONLY two things Service Control's
Kubernetes manager is allowed to do: read Deployment specs/status, and
patch the `scale` subresource. There is no `deployments` PATCH anywhere in
this file (verified by T5's invariant scan) -- that verb is a pod-template
write (command, secret mounts, serviceAccountName), which the RBAC Role
this adapter runs under deliberately never grants (D7).

Restart is implemented as scale-to-0, a bounded wait for pods to actually
terminate, then scale back to the remembered count -- never a rolling
restart, because a rolling restart needs `deployments` PATCH. The
scale-back always runs in a `finally` under `asyncio.shield` so no
exception, timeout, or task cancellation can ever leave a Deployment
stranded at 0 while this process is alive (D11). Process death mid-wait is
NOT covered here; that's surfaced as `restart_interrupted` by the envelope
builder in service_managers.py, which reads the lease this module's caller
(service_control.py) acquires around the whole action.

The bearer token is re-read from disk on every request (rotation-safe) and
never appears in any exception message, `repr`, or log event -- messages
are built from `kind` + Deployment name + HTTP status only (xander L-1).
"""
import asyncio
import ipaddress
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, FrozenSet, Optional

from shared.config import get_config

import httpx
import structlog

logger = structlog.get_logger()

# RFC1123 label -- the same shape Kubernetes itself enforces on Deployment
# (and therefore Service/Deployment-derived host) names.
DEPLOYMENT_NAME_RE = re.compile(r'^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$')

_SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TOKEN_PATH_DEFAULT = f"{_SERVICE_ACCOUNT_DIR}/token"
CA_PATH_DEFAULT = f"{_SERVICE_ACCOUNT_DIR}/ca.crt"
NAMESPACE_PATH_DEFAULT = f"{_SERVICE_ACCOUNT_DIR}/namespace"

DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_WAIT_TIMEOUT_SECONDS = 60.0
DEFAULT_REPLICA_CLAMP = (1, 10)


class K8sControlError(Exception):
    """kind in {forbidden, not_found, conflict, api_error, unavailable, invalid_name}.

    `message` is user-safe by construction: built from `kind` + Deployment
    name + HTTP status only. Never interpolate a raw httpx exception's str()
    (it can echo the request URL, which never contains the token, but this
    keeps the contract simple and auditable) and never `raise ... from`
    the underlying httpx exception -- `raise ... from None` everywhere so
    the token-bearing request object never rides along in `__cause__`.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class DeploymentInfo:
    name: str
    replicas: int
    ready_replicas: int


@dataclass
class ScaleStatus:
    spec_replicas: int
    status_replicas: int
    resource_version: Optional[str]


@dataclass
class ActionResult:
    success: bool
    message: str
    # restart only: 'ok' (scale-back PATCH landed), 'skipped' (lease lost,
    # scale-back deliberately not attempted), 'failed' (PATCH itself
    # errored), or None for stop/start (codex diff review r1 Critical #3/#4)
    scaleback: Optional[str] = None


def _bracket_if_ipv6(host: str) -> str:
    try:
        ipaddress.IPv6Address(host)
        return f"[{host}]"
    except ValueError:
        return host


def _status_to_kind(status_code: int) -> str:
    if status_code in (401, 403):
        return "forbidden"
    if status_code == 404:
        return "not_found"
    if status_code == 409:
        return "conflict"
    return "api_error"  # 422, 429, 5xx, and anything else non-2xx


class K8sDeploymentClient:
    """Scale-only Deployment client (D6). Every method validates `name`
    (regex + not protected) before building a URL. No method ever sends a
    request to `/deployments/{name}` with any method other than the list
    GET; PATCH/PUT only ever targets `/scale`."""

    def __init__(
        self,
        api_base: str,
        namespace: str,
        token_path: str = TOKEN_PATH_DEFAULT,
        ca_path: str = CA_PATH_DEFAULT,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        protected_names: FrozenSet[str] = frozenset(),
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
        timeout: float = 5.0,
    ):
        self._api_base = api_base.rstrip("/")
        self._namespace = namespace
        self._token_path = token_path
        self._ca_path = ca_path
        self._transport = transport
        self._protected_names = protected_names
        self._clock = clock
        self._sleep_fn = sleep_fn
        self._poll_interval = poll_interval
        self._wait_timeout = wait_timeout
        self._timeout = timeout
        self._ssl_context = self._build_ssl_context()

    @property
    def namespace(self) -> str:
        return self._namespace

    def _build_ssl_context(self):
        import ssl
        try:
            return ssl.create_default_context(cafile=self._ca_path)
        except (FileNotFoundError, OSError):
            # No CA file (e.g. test construction) -- caller supplies a
            # transport, so no real TLS handshake ever happens.
            return ssl.create_default_context()

    def _read_token(self) -> str:
        """Re-read on every request (token rotation, D6)."""
        return Path(self._token_path).read_text().strip()

    def _validate_name(self, name: str) -> None:
        if name in self._protected_names:
            raise K8sControlError("invalid_name", f"'{name}' is a protected Deployment and cannot be controlled")
        if not name or not DEPLOYMENT_NAME_RE.match(name):
            raise K8sControlError("invalid_name", f"'{name}' is not a valid Deployment name")

    def _client(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self._read_token()}"}
        kwargs: dict = {"base_url": self._api_base, "headers": headers, "timeout": self._timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        else:
            kwargs["verify"] = self._ssl_context
        return httpx.AsyncClient(**kwargs)

    async def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> httpx.Response:
        try:
            async with self._client() as client:
                if method == "GET":
                    response = await client.get(path)
                elif method == "PATCH":
                    response = await client.patch(
                        path, json=json_body,
                        headers={"Content-Type": "application/merge-patch+json"},
                    )
                else:  # pragma: no cover — defensive, only GET/PATCH are used
                    raise ValueError(f"unsupported method {method}")
        except httpx.TimeoutException:
            raise K8sControlError("unavailable", "Kubernetes API request timed out") from None
        except httpx.ConnectError:
            raise K8sControlError("unavailable", "Kubernetes API is unreachable") from None
        except httpx.HTTPError as exc:
            raise K8sControlError("unavailable", f"Kubernetes API request failed: {type(exc).__name__}") from None
        return response

    async def list_deployments(self) -> list:
        path = f"/apis/apps/v1/namespaces/{self._namespace}/deployments"
        response = await self._request("GET", path)
        if response.status_code != 200:
            raise K8sControlError(_status_to_kind(response.status_code), f"list_deployments failed ({response.status_code})")
        body = response.json()
        results = []
        for item in body.get("items", []):
            name = item.get("metadata", {}).get("name", "")
            spec_replicas = item.get("spec", {}).get("replicas", 0) or 0
            ready = item.get("status", {}).get("readyReplicas", 0) or 0
            results.append(DeploymentInfo(name=name, replicas=spec_replicas, ready_replicas=ready))
        return results

    async def get_scale(self, name: str) -> ScaleStatus:
        self._validate_name(name)
        path = f"/apis/apps/v1/namespaces/{self._namespace}/deployments/{name}/scale"
        response = await self._request("GET", path)
        if response.status_code != 200:
            raise K8sControlError(_status_to_kind(response.status_code), f"get_scale('{name}') failed ({response.status_code})")
        body = response.json()
        return ScaleStatus(
            spec_replicas=body.get("spec", {}).get("replicas", 0) or 0,
            status_replicas=body.get("status", {}).get("replicas", 0) or 0,
            resource_version=body.get("metadata", {}).get("resourceVersion"),
        )

    async def set_replicas(self, name: str, n: int, resource_version: Optional[str] = None) -> None:
        self._validate_name(name)
        path = f"/apis/apps/v1/namespaces/{self._namespace}/deployments/{name}/scale"
        body: dict = {"spec": {"replicas": n}}
        if resource_version is not None:
            body["metadata"] = {"resourceVersion": resource_version}
        response = await self._request("PATCH", path, json_body=body)
        if response.status_code != 200:
            raise K8sControlError(_status_to_kind(response.status_code), f"set_replicas('{name}', {n}) failed ({response.status_code})")

    async def stop(self, name: str, remember: Callable[[int], None]) -> ActionResult:
        self._validate_name(name)
        scale = await self.get_scale(name)
        if scale.spec_replicas == 0:
            return ActionResult(success=True, message="already stopped")
        remember(scale.spec_replicas)
        try:
            await self.set_replicas(name, 0, resource_version=scale.resource_version)
        except K8sControlError as exc:
            if exc.kind == "conflict":
                return ActionResult(success=False, message="changed concurrently, reload and retry")
            raise
        return ActionResult(success=True, message="stopped")

    async def start(self, name: str, recall: Callable[[], int]) -> ActionResult:
        self._validate_name(name)
        n = recall()
        scale = await self.get_scale(name)
        try:
            await self.set_replicas(name, n, resource_version=scale.resource_version)
        except K8sControlError as exc:
            if exc.kind == "conflict":
                return ActionResult(success=False, message="changed concurrently, reload and retry")
            raise
        return ActionResult(success=True, message="started")

    async def restart(
        self,
        name: str,
        remember: Callable[[int], None],
        recall: Callable[[], int],
        still_owner: Optional[Callable[[], bool]] = None,
    ) -> ActionResult:
        self._validate_name(name)
        scale = await self.get_scale(name)
        n = scale.spec_replicas
        if n == 0:
            return ActionResult(success=False, message="cannot restart: already at 0 replicas")

        remember(n)
        await self.set_replicas(name, 0, resource_version=scale.resource_version)

        settled = False
        scaleback = 'ok'
        try:
            start_time = self._clock()
            while (self._clock() - start_time) < self._wait_timeout:
                await self._sleep_fn(self._poll_interval)
                current = await self.get_scale(name)
                if current.status_replicas == 0:
                    settled = True
                    break
        finally:
            # D11: the scale-back MUST land regardless of how we got here
            # (timeout, exception, or the caller's task being cancelled).
            # asyncio.shield means a cancellation of the code awaiting this
            # `finally` does not cancel the PATCH itself. codex diff review
            # r1 Critical #3: but ONLY when we still hold the lease -- if
            # another replica has taken over (still_owner() false), a
            # scale-back here would race and possibly resurrect a
            # deployment the new holder already stopped on purpose.
            if still_owner is not None and not still_owner():
                scaleback = 'skipped'
            else:
                try:
                    await asyncio.shield(self.set_replicas(name, n))
                except Exception:  # noqa: BLE001 -- reported via scaleback, not re-raised
                    scaleback = 'failed'

        if scaleback == 'skipped':
            return ActionResult(
                success=False,
                message="restart superseded by another admin-backend replica; scale-back skipped",
                scaleback=scaleback,
            )
        if scaleback == 'failed':
            return ActionResult(
                success=False,
                message=f"pods terminated but the scale-back to {n} replicas failed",
                scaleback=scaleback,
            )
        if settled:
            return ActionResult(success=True, message="restarted", scaleback=scaleback)
        return ActionResult(
            success=False,
            message=f"pods did not terminate within {int(self._wait_timeout)}s; scaled back to {n}",
            scaleback=scaleback,
        )


_client_cache: Optional[tuple] = None  # (client_or_None, reason_or_None)

# Test-injection hook (mirrors service_managers._ca_transport): when set,
# get_k8s_client() builds its client with this transport instead of a real
# network client, so route-level tests can exercise the full
# _run_action -> get_k8s_client -> K8sDeploymentClient path end to end.
_test_transport: Optional[httpx.AsyncBaseTransport] = None


def _clear_client_cache() -> None:
    """Test-only helper."""
    global _client_cache
    _client_cache = None


def get_k8s_client(force: bool = False):
    """Factory: (client|None, reason|None). Memoized; logs the reason once
    at WARNING when the flag is enabled but the client can't be built.
    Never touches the filesystem when the flag is off (D14)."""
    global _client_cache
    if _client_cache is not None and not force:
        return _client_cache

    if not get_config().service_control_k8s_enabled:
        _client_cache = (None, "disabled")
        return _client_cache

    host = os.getenv("KUBERNETES_SERVICE_HOST")
    port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        _client_cache = (None, "not_in_cluster")
        logger.warning("k8s_client_unavailable", reason="not_in_cluster")
        return _client_cache

    if not Path(TOKEN_PATH_DEFAULT).exists():
        _client_cache = (None, "no_service_account_token")
        logger.warning("k8s_client_unavailable", reason="no_service_account_token")
        return _client_cache

    namespace = os.getenv("ATHENA_NAMESPACE", "athena-prod")
    ns_path = Path(NAMESPACE_PATH_DEFAULT)
    if ns_path.exists():
        try:
            namespace = ns_path.read_text().strip() or namespace
        except OSError:
            pass

    from app.services.service_managers import PROTECTED_DEPLOYMENTS

    api_base = f"https://{_bracket_if_ipv6(host)}:{port}"
    client = K8sDeploymentClient(
        api_base=api_base,
        namespace=namespace,
        token_path=TOKEN_PATH_DEFAULT,
        ca_path=CA_PATH_DEFAULT,
        transport=_test_transport,
        protected_names=frozenset(PROTECTED_DEPLOYMENTS),
    )
    _client_cache = (client, None)
    return _client_cache
