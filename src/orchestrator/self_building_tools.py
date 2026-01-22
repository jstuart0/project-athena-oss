"""
Self-Building Tools System for Project Athena

Allows the LLM to create new n8n workflows dynamically when it detects
a capability gap. This is an experimental feature controlled by feature flag.

Architecture:
    1. LLM identifies a capability gap during conversation
    2. LLM generates an n8n workflow definition
    3. Workflow is submitted for approval (owner must approve)
    4. Approved workflow is deployed to n8n
    5. New tool becomes available via MCP discovery

Security:
    - All generated workflows require owner approval
    - Workflows are sandboxed in n8n
    - Rate limited to prevent abuse
    - Audit logged for traceability

Usage:
    from orchestrator.self_building_tools import SelfBuildingToolsManager

    manager = SelfBuildingToolsManager()
    result = await manager.propose_tool(
        name="check_package_status",
        description="Check shipping status of a package",
        trigger_phrases=["track package", "where is my package"],
        workflow_definition={...}
    )
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
import httpx
import structlog

logger = structlog.get_logger()


class ToolProposalStatus(Enum):
    """Status of a proposed tool."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPLOYED = "deployed"
    FAILED = "failed"


@dataclass
class ToolProposal:
    """A proposed new tool definition."""
    id: str
    name: str
    description: str
    trigger_phrases: List[str]
    workflow_definition: Dict[str, Any]
    status: ToolProposalStatus = ToolProposalStatus.PENDING
    created_at: float = field(default_factory=time.time)
    created_by: str = "llm"
    approved_by: Optional[str] = None
    approved_at: Optional[float] = None
    rejection_reason: Optional[str] = None
    n8n_workflow_id: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'trigger_phrases': self.trigger_phrases,
            'workflow_definition': self.workflow_definition,
            'status': self.status.value,
            'created_at': self.created_at,
            'created_at_iso': datetime.fromtimestamp(self.created_at).isoformat(),
            'created_by': self.created_by,
            'approved_by': self.approved_by,
            'approved_at': self.approved_at,
            'rejection_reason': self.rejection_reason,
            'n8n_workflow_id': self.n8n_workflow_id,
            'error_message': self.error_message,
        }


class N8nWorkflowBuilder:
    """
    Builds n8n workflow definitions from LLM specifications.

    Converts high-level tool descriptions into n8n-compatible
    workflow JSON with proper node structure.
    """

    @staticmethod
    def create_webhook_trigger(tool_name: str) -> Dict[str, Any]:
        """Create a webhook trigger node for the workflow."""
        return {
            "id": "webhook-trigger",
            "name": "Webhook",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 1,
            "position": [250, 300],
            "parameters": {
                "path": f"athena-tool-{tool_name}",
                "httpMethod": "POST",
                "responseMode": "lastNode",
                "options": {}
            }
        }

    @staticmethod
    def create_http_request_node(
        node_id: str,
        name: str,
        url: str,
        method: str = "GET",
        position: List[int] = None,
        query_params: Dict[str, str] = None
    ) -> Dict[str, Any]:
        """Create an HTTP request node."""
        node = {
            "id": node_id,
            "name": name,
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4,
            "position": position or [450, 300],
            "parameters": {
                "url": url,
                "method": method,
                "options": {}
            }
        }

        # Add query parameters if provided
        if query_params:
            node["parameters"]["sendQuery"] = True
            node["parameters"]["queryParameters"] = {
                "parameters": [
                    {"name": k, "value": v} for k, v in query_params.items()
                ]
            }

        return node

    @staticmethod
    def create_code_node(
        node_id: str,
        name: str,
        code: str,
        position: List[int] = None
    ) -> Dict[str, Any]:
        """Create a JavaScript code node."""
        return {
            "id": node_id,
            "name": name,
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": position or [650, 300],
            "parameters": {
                "jsCode": code,
                "mode": "runOnceForAllItems"
            }
        }

    @staticmethod
    def create_respond_node(position: List[int] = None) -> Dict[str, Any]:
        """Create a response node to return data to webhook caller."""
        return {
            "id": "respond-webhook",
            "name": "Respond to Webhook",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": position or [850, 300],
            "parameters": {
                "respondWith": "allIncomingItems",
                "options": {}
            }
        }

    @classmethod
    def build_simple_api_workflow(
        cls,
        tool_name: str,
        api_url: str,
        api_method: str = "GET",
        transform_code: Optional[str] = None,
        query_params: Optional[Dict[str, str]] = None,
        required_api_key: Optional[str] = None,
        api_key_param: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Build a simple workflow that calls an API and returns the result.

        Args:
            tool_name: Name of the tool
            api_url: URL to call
            api_method: HTTP method
            transform_code: Optional JS code to transform the response
            query_params: Query parameters using n8n expressions
            required_api_key: Service name if API key needed (e.g., "openweathermap")
            api_key_param: Query param name for API key (e.g., "appid")
        """
        # Create webhook with webhookId for proper registration
        webhook_node = cls.create_webhook_trigger(tool_name)
        webhook_node["webhookId"] = f"athena-{tool_name}"

        # Build query params, including API key placeholder if needed
        final_query_params = dict(query_params) if query_params else {}
        if required_api_key and api_key_param:
            # Add placeholder - will be replaced during deployment with real key
            final_query_params[api_key_param] = f"{{{{ATHENA_API_KEY:{required_api_key}}}}}"

        nodes = [
            webhook_node,
            cls.create_http_request_node(
                "api-call", "API Request", api_url, api_method, [450, 300],
                query_params=final_query_params if final_query_params else None
            ),
        ]

        connections = {
            "Webhook": {
                "main": [[{"node": "API Request", "type": "main", "index": 0}]]
            }
        }

        if transform_code:
            nodes.append(cls.create_code_node("transform", "Transform", transform_code, [650, 300]))
            nodes.append(cls.create_respond_node([850, 300]))
            connections["API Request"] = {
                "main": [[{"node": "Transform", "type": "main", "index": 0}]]
            }
            connections["Transform"] = {
                "main": [[{"node": "Respond to Webhook", "type": "main", "index": 0}]]
            }
        else:
            nodes.append(cls.create_respond_node([650, 300]))
            connections["API Request"] = {
                "main": [[{"node": "Respond to Webhook", "type": "main", "index": 0}]]
            }

        return {
            "name": f"Athena Tool: {tool_name}",
            "nodes": nodes,
            "connections": connections,
            "settings": {
                "executionOrder": "v1"
            },
            "staticData": None,
            # Store metadata for deployment
            "_athena_metadata": {
                "required_api_key": required_api_key,
                "api_key_param": api_key_param
            }
        }


class SelfBuildingToolsManager:
    """
    Manages the self-building tools lifecycle.

    Handles:
    - Tool proposal creation
    - Approval workflow
    - n8n deployment
    - Tool registry integration
    """

    def __init__(
        self,
        n8n_url: str = None,
        n8n_api_key: str = None,
        admin_url: str = None
    ):
        # Use Thor's central n8n service by default
        self.n8n_url = n8n_url or os.getenv("N8N_URL", "http://localhost:5678")
        self.n8n_api_key = n8n_api_key or os.getenv("N8N_API_KEY", "")
        self.admin_url = admin_url or os.getenv("ADMIN_API_URL", "http://localhost:8080")

        self._proposals: Dict[str, ToolProposal] = {}
        self._enabled = False
        self._rate_limit_count = 0
        self._rate_limit_reset = time.time()
        self._max_proposals_per_hour = 10
        self._api_key_fetched = False

    async def _fetch_n8n_api_key(self) -> str:
        """Fetch n8n API key from Athena's external API key storage."""
        if self.n8n_api_key and self._api_key_fetched:
            return self.n8n_api_key

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.admin_url}/api/external-api-keys/public/n8n/key")
                if response.status_code == 200:
                    data = response.json()
                    self.n8n_api_key = data.get('api_key', '')
                    self.n8n_url = data.get('endpoint_url', self.n8n_url)
                    self._api_key_fetched = True
                    logger.info("n8n_api_key_fetched", url=self.n8n_url)
        except Exception as e:
            logger.warning("n8n_api_key_fetch_failed", error=str(e))

        return self.n8n_api_key

    async def check_enabled(self) -> bool:
        """Check if self-building tools is enabled via feature flag."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Use the public features endpoint and filter by name
                response = await client.get(f"{self.admin_url}/api/features/public")
                if response.status_code == 200:
                    features = response.json()
                    # Find self_building_tools feature in the list
                    for feature in features:
                        if feature.get('name') == 'self_building_tools':
                            self._enabled = feature.get('enabled', False)
                            logger.info("self_building_tools_check",
                                       enabled=self._enabled,
                                       feature_id=feature.get('id'))
                            return self._enabled
                    # Feature not found - disabled by default
                    logger.warning("self_building_tools_not_found")
                    self._enabled = False
        except Exception as e:
            logger.warning("self_building_check_failed", error=str(e))
            self._enabled = False

        return self._enabled

    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits."""
        now = time.time()
        if now - self._rate_limit_reset > 3600:  # Reset hourly
            self._rate_limit_count = 0
            self._rate_limit_reset = now

        return self._rate_limit_count < self._max_proposals_per_hour

    async def propose_tool(
        self,
        name: str,
        description: str,
        trigger_phrases: List[str],
        workflow_definition: Optional[Dict[str, Any]] = None,
        api_url: Optional[str] = None,
        api_method: str = "GET",
        transform_code: Optional[str] = None,
        query_params: Optional[Dict[str, str]] = None,
        required_api_key: Optional[str] = None,
        api_key_param: Optional[str] = None,
        created_by: str = "llm"
    ) -> Dict[str, Any]:
        """
        Propose a new tool for creation.

        Args:
            name: Tool name (lowercase, underscores)
            description: What the tool does
            trigger_phrases: Phrases that should invoke this tool
            workflow_definition: Full n8n workflow (optional)
            api_url: API URL for simple API tools
            api_method: HTTP method for API tools
            transform_code: JS code to transform API response
            query_params: Query parameters with n8n expressions
            required_api_key: Service name if API key needed
            api_key_param: Query param name for API key
            created_by: Who created this proposal

        Returns:
            Proposal details including ID and status
        """
        # Check if enabled
        if not await self.check_enabled():
            return {
                'success': False,
                'error': 'Self-building tools is disabled',
                'error_code': 'FEATURE_DISABLED'
            }

        # Check rate limit
        if not self._check_rate_limit():
            return {
                'success': False,
                'error': 'Rate limit exceeded. Try again later.',
                'error_code': 'RATE_LIMITED'
            }

        self._rate_limit_count += 1

        # Generate workflow if not provided
        if workflow_definition is None:
            if api_url:
                workflow_definition = N8nWorkflowBuilder.build_simple_api_workflow(
                    name, api_url, api_method, transform_code,
                    query_params=query_params,
                    required_api_key=required_api_key,
                    api_key_param=api_key_param
                )
            else:
                return {
                    'success': False,
                    'error': 'Either workflow_definition or api_url is required',
                    'error_code': 'MISSING_DEFINITION'
                }

        # Create proposal
        import uuid
        proposal_id = str(uuid.uuid4())[:8]

        proposal = ToolProposal(
            id=proposal_id,
            name=name,
            description=description,
            trigger_phrases=trigger_phrases,
            workflow_definition=workflow_definition,
            created_by=created_by
        )

        self._proposals[proposal_id] = proposal

        # Save to admin backend for approval
        try:
            await self._save_proposal(proposal)
        except Exception as e:
            logger.error("proposal_save_failed", error=str(e))

        logger.info("tool_proposed",
                   proposal_id=proposal_id,
                   name=name,
                   created_by=created_by)

        return {
            'success': True,
            'proposal_id': proposal_id,
            'name': name,
            'status': 'pending',
            'message': 'Tool proposal submitted for approval'
        }

    async def _save_proposal(self, proposal: ToolProposal):
        """Save proposal to admin backend."""
        try:
            # Build the payload matching ToolProposalCreate model
            payload = {
                "name": proposal.name,
                "description": proposal.description,
                "trigger_phrases": proposal.trigger_phrases,
                "workflow_definition": proposal.workflow_definition,
                "created_by": proposal.created_by or "llm"
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self.admin_url}/api/tool-proposals",
                    json=payload
                )
                if response.status_code not in (200, 201):
                    logger.warning("proposal_save_warning",
                                  status=response.status_code,
                                  response_text=response.text[:200])
                else:
                    logger.info("proposal_saved_to_backend",
                               proposal_id=proposal.id,
                               name=proposal.name)
        except Exception as e:
            logger.warning("proposal_save_failed", error=str(e))

    async def approve_proposal(
        self,
        proposal_id: str,
        approved_by: str
    ) -> Dict[str, Any]:
        """
        Approve a tool proposal and deploy to n8n.

        Args:
            proposal_id: ID of the proposal
            approved_by: Username of approver

        Returns:
            Deployment result
        """
        proposal = self._proposals.get(proposal_id)
        if not proposal:
            return {
                'success': False,
                'error': 'Proposal not found',
                'error_code': 'NOT_FOUND'
            }

        if proposal.status != ToolProposalStatus.PENDING:
            return {
                'success': False,
                'error': f'Proposal is {proposal.status.value}, not pending',
                'error_code': 'INVALID_STATUS'
            }

        proposal.status = ToolProposalStatus.APPROVED
        proposal.approved_by = approved_by
        proposal.approved_at = time.time()

        # Deploy to n8n
        try:
            workflow_id = await self._deploy_to_n8n(proposal)
            proposal.n8n_workflow_id = workflow_id
            proposal.status = ToolProposalStatus.DEPLOYED

            logger.info("tool_deployed",
                       proposal_id=proposal_id,
                       workflow_id=workflow_id,
                       approved_by=approved_by)

            return {
                'success': True,
                'proposal_id': proposal_id,
                'status': 'deployed',
                'n8n_workflow_id': workflow_id
            }

        except Exception as e:
            proposal.status = ToolProposalStatus.FAILED
            proposal.error_message = str(e)

            logger.error("tool_deployment_failed",
                        proposal_id=proposal_id,
                        error=str(e))

            return {
                'success': False,
                'error': str(e),
                'error_code': 'DEPLOYMENT_FAILED'
            }

    async def reject_proposal(
        self,
        proposal_id: str,
        rejected_by: str,
        reason: str
    ) -> Dict[str, Any]:
        """Reject a tool proposal."""
        proposal = self._proposals.get(proposal_id)
        if not proposal:
            return {'success': False, 'error': 'Proposal not found'}

        proposal.status = ToolProposalStatus.REJECTED
        proposal.rejection_reason = reason

        logger.info("tool_rejected",
                   proposal_id=proposal_id,
                   rejected_by=rejected_by,
                   reason=reason)

        return {
            'success': True,
            'proposal_id': proposal_id,
            'status': 'rejected'
        }

    async def _deploy_to_n8n(self, proposal: ToolProposal) -> str:
        """Deploy workflow to n8n and return workflow ID."""
        # Fetch API key from Athena storage if not already set
        await self._fetch_n8n_api_key()

        if not self.n8n_api_key:
            raise ValueError("N8N_API_KEY not configured (check Athena external-api-keys or N8N_API_KEY env var)")

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Create workflow
            response = await client.post(
                f"{self.n8n_url}/api/v1/workflows",
                headers={
                    "X-N8N-API-KEY": self.n8n_api_key,
                    "Content-Type": "application/json"
                },
                json=proposal.workflow_definition
            )

            if response.status_code not in (200, 201):
                raise RuntimeError(f"n8n workflow creation failed: {response.text}")

            data = response.json()
            workflow_id = data.get('id')

            # Activate workflow
            await client.patch(
                f"{self.n8n_url}/api/v1/workflows/{workflow_id}",
                headers={"X-N8N-API-KEY": self.n8n_api_key},
                json={"active": True}
            )

            return workflow_id

    def list_proposals(
        self,
        status: Optional[ToolProposalStatus] = None
    ) -> List[Dict[str, Any]]:
        """List all proposals, optionally filtered by status."""
        proposals = self._proposals.values()
        if status:
            proposals = [p for p in proposals if p.status == status]
        return [p.to_dict() for p in proposals]

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific proposal."""
        proposal = self._proposals.get(proposal_id)
        return proposal.to_dict() if proposal else None

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about self-building tools."""
        proposals_by_status = {}
        for status in ToolProposalStatus:
            proposals_by_status[status.value] = len([
                p for p in self._proposals.values() if p.status == status
            ])

        return {
            'enabled': self._enabled,
            'total_proposals': len(self._proposals),
            'proposals_by_status': proposals_by_status,
            'rate_limit_remaining': self._max_proposals_per_hour - self._rate_limit_count,
            'rate_limit_reset': datetime.fromtimestamp(self._rate_limit_reset + 3600).isoformat(),
        }


# Factory for dependency injection
class SelfBuildingToolsFactory:
    """Factory for self-building tools manager."""

    _instance: Optional[SelfBuildingToolsManager] = None

    @classmethod
    def get(cls) -> SelfBuildingToolsManager:
        """Get or create manager instance."""
        if cls._instance is None:
            cls._instance = SelfBuildingToolsManager()
        return cls._instance

    @classmethod
    def clear(cls):
        """Clear instance (for testing)."""
        cls._instance = None


# LLM Integration - Prompt template for tool generation
TOOL_GENERATION_PROMPT = """
You are helping to create a new tool for the Athena voice assistant.

Based on the user's request, generate a tool definition:

User Request: {user_request}

Generate a JSON response with:
{{
    "name": "tool_name_in_snake_case",
    "description": "What this tool does",
    "trigger_phrases": ["phrase 1", "phrase 2", "phrase 3"],
    "api_url": "https://api.example.com/endpoint",
    "api_method": "GET",
    "query_params": {{"param1": "{{{{$json.body.param1}}}}", "q": "{{{{$json.body.query}}}}"}},
    "required_api_key": "service_name or null",
    "api_key_param": "appid or apiKey or null",
    "transform_code": "const data = items[0].json;\\nreturn [{{json: {{result: data.main_field, formatted: `Summary: ${{data.value}}`}}}}];"
}}

Rules:
1. Name must be lowercase with underscores
2. Description should be 1-2 sentences
3. Include 3-5 trigger phrases users might say
4. API URL must be a real, publicly accessible API (no auth required, or common free APIs)
5. query_params: Use n8n expressions like {{{{$json.body.fieldname}}}} to reference webhook input
6. required_api_key: If API needs a key, specify service name (e.g., "openweathermap", "google_places") or null
7. api_key_param: The query parameter name for the API key (e.g., "appid", "key", "apiKey") or null
8. transform_code: JavaScript to format the API response into a clean result

Example for weather:
{{
    "name": "weather_lookup",
    "description": "Get current weather for a location",
    "trigger_phrases": ["what's the weather", "weather in", "temperature in"],
    "api_url": "https://api.openweathermap.org/data/2.5/weather",
    "api_method": "GET",
    "query_params": {{"q": "{{{{$json.body.location}}}}", "units": "imperial"}},
    "required_api_key": "openweathermap",
    "api_key_param": "appid",
    "transform_code": "const w = items[0].json;\\nreturn [{{json: {{temperature: w.main.temp, description: w.weather[0].description, location: w.name}}}}];"
}}

Respond with ONLY the JSON, no markdown or explanation.
"""


async def generate_tool_from_request(
    user_request: str,
    llm_router: Any = None,
    model: str = "llama3.1:8b"
) -> Dict[str, Any]:
    """
    Use LLM to generate a tool definition from natural language.

    Args:
        user_request: What the user wants the tool to do
        llm_router: LLMRouter instance (if None, will import and get one)
        model: LLM model to use

    Returns:
        Parsed tool definition or error
    """
    prompt = TOOL_GENERATION_PROMPT.format(user_request=user_request)

    try:
        # Get LLM router if not provided
        if llm_router is None:
            from shared.llm_router import get_llm_router
            llm_router = get_llm_router()

        # Call LLM using LLMRouter.generate()
        response = await llm_router.generate(
            model=model,
            prompt=prompt,
            temperature=0.3,
            max_tokens=1000
        )

        # LLMRouter returns 'response' for Ollama backend, 'content' for others
        content = response.get('response', '') or response.get('content', '')

        logger.info("tool_generation_llm_response",
                   model=model,
                   response_keys=list(response.keys()),
                   content_length=len(content) if content else 0,
                   content_preview=content[:200] if content else "empty")

        # Try to extract JSON from the response
        # LLM might include markdown code blocks
        if '```json' in content:
            content = content.split('```json')[1].split('```')[0].strip()
        elif '```' in content:
            content = content.split('```')[1].split('```')[0].strip()

        # Parse JSON from response
        tool_def = json.loads(content)

        # Validate required fields
        required = ['name', 'description', 'trigger_phrases']
        for field in required:
            if field not in tool_def:
                return {'success': False, 'error': f'Missing required field: {field}'}

        return {
            'success': True,
            'tool_definition': tool_def
        }

    except json.JSONDecodeError as e:
        logger.error("tool_generation_json_error", error=str(e), content=content[:200] if content else "empty")
        return {'success': False, 'error': f'Invalid JSON from LLM: {str(e)}'}
    except Exception as e:
        logger.error("tool_generation_error", error=str(e))
        return {'success': False, 'error': str(e)}
