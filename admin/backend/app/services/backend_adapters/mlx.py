from __future__ import annotations

from collections import defaultdict
from typing import List

import httpx
from sqlalchemy.orm import Session

from app.models import LLMBackend
from app.services.backend_adapters.base import BackendCapabilities, ModelDescriptor


class MLXAdapter:
    def __init__(self, db: Session):
        self.db = db

    async def collect(self) -> List[BackendCapabilities]:
        backends = self.db.query(LLMBackend).filter(
            LLMBackend.enabled == True,
            LLMBackend.backend_type == "mlx",
        ).all()
        grouped = defaultdict(list)
        for backend in backends:
            grouped[backend.endpoint_url].append(backend)

        capabilities: List[BackendCapabilities] = []
        for endpoint_url, models in grouped.items():
            healthy = False
            served_names = set()
            errors = []
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(f"{endpoint_url.rstrip('/')}/v1/models")
                    response.raise_for_status()
                    payload = response.json()
                healthy = True
                for item in payload.get("data", []):
                    if item.get("id"):
                        served_names.add(item["id"])
            except Exception as exc:
                errors.append(str(exc))

            descriptors = []
            for backend in models:
                model_name = backend.model_name
                served = model_name in served_names or not served_names
                descriptors.append(ModelDescriptor(
                    name=model_name,
                    backend_type="mlx",
                    endpoint_url=endpoint_url,
                    family="qwen3" if "qwen3" in model_name.lower() else None,
                    status="served" if healthy and served else "configured",
                    installability_state="configured",
                    supports_runtime_options=True,
                    supports_hot_reload=True,
                    metadata={"configured_only": not (healthy and served)},
                ))

            capabilities.append(BackendCapabilities(
                backend_key=f"mlx:{endpoint_url}",
                backend_type="mlx",
                endpoint_url=endpoint_url,
                display_name="MLX",
                healthy=healthy,
                status="healthy" if healthy else "degraded",
                supports_install_action=False,
                supports_hot_reload=True,
                models=descriptors,
                errors=errors,
            ))

        return capabilities
