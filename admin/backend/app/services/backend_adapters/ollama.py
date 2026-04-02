from __future__ import annotations

from typing import List

import httpx
from sqlalchemy.orm import Session

from app.models import LLMBackend
from app.services.backend_adapters.base import BackendCapabilities, ModelDescriptor


class OllamaAdapter:
    def __init__(self, db: Session, endpoint_url: str):
        self.db = db
        self.endpoint_url = endpoint_url

    async def collect(self) -> List[BackendCapabilities]:
        configured = self.db.query(LLMBackend).filter(
            LLMBackend.enabled == True,
            LLMBackend.backend_type == "ollama",
        ).all()

        installed_by_name = {}
        errors = []
        healthy = False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.endpoint_url}/api/tags")
                response.raise_for_status()
                payload = response.json()
            healthy = True
            for model in payload.get("models", []):
                details = model.get("details", {}) or {}
                installed_by_name[model.get("name")] = ModelDescriptor(
                    name=model.get("name"),
                    backend_type="ollama",
                    endpoint_url=self.endpoint_url,
                    family=details.get("family"),
                    status="installed",
                    installability_state="installed",
                    supports_runtime_options=True,
                    supports_hot_reload=False,
                    metadata={
                        "size": model.get("size"),
                        "parameter_size": details.get("parameter_size"),
                        "quantization": details.get("quantization_level"),
                    },
                )
        except Exception as exc:
            errors.append(str(exc))

        models = list(installed_by_name.values())
        configured_names = {backend.model_name for backend in configured}
        for backend in configured:
            if backend.model_name not in installed_by_name:
                models.append(ModelDescriptor(
                    name=backend.model_name,
                    backend_type="ollama",
                    endpoint_url=backend.endpoint_url,
                    family=None,
                    status="configured",
                    installability_state="installable",
                    supports_runtime_options=True,
                    supports_hot_reload=False,
                    metadata={"configured_only": True},
                ))

        for curated in ["qwen3:4b-instruct-2507-q4_K_M", "qwen3:8b"]:
            if curated not in configured_names and curated not in installed_by_name:
                models.append(ModelDescriptor(
                    name=curated,
                    backend_type="ollama",
                    endpoint_url=self.endpoint_url,
                    family="qwen3",
                    status="unavailable" if not healthy else "configured",
                    installability_state="installable",
                    supports_runtime_options=True,
                    supports_hot_reload=False,
                    metadata={"curated": True},
                ))

        return [BackendCapabilities(
            backend_key=f"ollama:{self.endpoint_url}",
            backend_type="ollama",
            endpoint_url=self.endpoint_url,
            display_name="Ollama",
            healthy=healthy,
            status="healthy" if healthy else "unreachable",
            supports_install_action=True,
            supports_hot_reload=False,
            models=models,
            errors=errors,
        )]
