from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class ModelDescriptor:
    name: str
    backend_type: str
    endpoint_url: Optional[str]
    family: Optional[str] = None
    status: str = "configured"  # installed, served, configured, unavailable
    installability_state: str = "unavailable"  # installed, installable, configured, unavailable
    supports_runtime_options: bool = True
    supports_hot_reload: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BackendHealthResult:
    backend_key: str
    backend_type: str
    endpoint_url: Optional[str]
    healthy: bool
    status: str
    display_name: str
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BackendCapabilities:
    backend_key: str
    backend_type: str
    endpoint_url: Optional[str]
    display_name: str
    healthy: bool
    status: str
    supports_install_action: bool
    supports_hot_reload: bool
    models: List[ModelDescriptor] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["models"] = [model.to_dict() for model in self.models]
        return payload


class BackendAdapter(Protocol):
    async def collect(self) -> List[BackendCapabilities]:
        ...
