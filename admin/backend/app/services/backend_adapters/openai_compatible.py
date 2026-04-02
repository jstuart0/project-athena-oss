from __future__ import annotations

from collections import defaultdict
from typing import List

import httpx
from sqlalchemy.orm import Session

from app.models import LLMBackend
from app.services.backend_adapters.base import BackendCapabilities, ModelDescriptor


class OpenAICompatibleAdapter:
    def __init__(self, db: Session):
        self.db = db

    async def collect(self) -> List[BackendCapabilities]:
        backends = self.db.query(LLMBackend).filter(
            LLMBackend.enabled == True,
            ~LLMBackend.backend_type.in_(["ollama", "mlx", "auto"]),
        ).all()
        grouped = defaultdict(list)
        for backend in backends:
            grouped[(backend.backend_type, backend.endpoint_url)].append(backend)

        capabilities: List[BackendCapabilities] = []
        for (backend_type, endpoint_url), models in grouped.items():
            healthy = False
            known_models = set()
            errors = []
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(f"{endpoint_url.rstrip('/')}/v1/models")
                    response.raise_for_status()
                    payload = response.json()
                healthy = True
                for item in payload.get("data", []):
                    if item.get("id"):
                        known_models.add(item["id"])
            except Exception as exc:
                errors.append(str(exc))

            descriptors = []
            for backend in models:
                descriptors.append(ModelDescriptor(
                    name=backend.model_name,
                    backend_type=backend_type,
                    endpoint_url=endpoint_url,
                    family=backend_type,
                    status="served" if healthy and (not known_models or backend.model_name in known_models) else "configured",
                    installability_state="configured",
                    supports_runtime_options=False,
                    supports_hot_reload=False,
                    metadata={"catalog_known": backend.model_name in known_models if known_models else None},
                ))

            capabilities.append(BackendCapabilities(
                backend_key=f"{backend_type}:{endpoint_url}",
                backend_type=backend_type,
                endpoint_url=endpoint_url,
                display_name=f"{backend_type.title()} Compatible",
                healthy=healthy,
                status="healthy" if healthy else "degraded",
                supports_install_action=False,
                supports_hot_reload=False,
                models=descriptors,
                errors=errors,
            ))
        return capabilities
