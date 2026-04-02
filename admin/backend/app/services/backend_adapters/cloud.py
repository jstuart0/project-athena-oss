from __future__ import annotations

from typing import List

from sqlalchemy.orm import Session

from app.models import CloudLLMProvider, ExternalAPIKey
from app.services.backend_adapters.base import BackendCapabilities, ModelDescriptor


class CloudAdapter:
    def __init__(self, db: Session):
        self.db = db

    async def collect(self) -> List[BackendCapabilities]:
        providers = self.db.query(CloudLLMProvider).filter(CloudLLMProvider.enabled == True).all()
        capabilities: List[BackendCapabilities] = []
        for provider in providers:
            api_key = self.db.query(ExternalAPIKey).filter(
                ExternalAPIKey.service_name == provider.provider,
                ExternalAPIKey.enabled == True,
            ).first()
            healthy = api_key is not None
            model_name = provider.default_model or f"{provider.provider}/default"
            capabilities.append(BackendCapabilities(
                backend_key=f"cloud:{provider.provider}",
                backend_type="cloud",
                endpoint_url=None,
                display_name=provider.display_name,
                healthy=healthy,
                status="healthy" if healthy else "degraded",
                supports_install_action=False,
                supports_hot_reload=False,
                models=[ModelDescriptor(
                    name=model_name,
                    backend_type="cloud",
                    endpoint_url=None,
                    family=provider.provider,
                    status="configured" if healthy else "unavailable",
                    installability_state="configured",
                    supports_runtime_options=False,
                    supports_hot_reload=False,
                    metadata={"provider": provider.provider, "has_api_key": healthy},
                )],
                errors=[] if healthy else [f"Missing API key for {provider.provider}"],
                metadata={"provider": provider.provider, "has_api_key": healthy},
            ))
        return capabilities
