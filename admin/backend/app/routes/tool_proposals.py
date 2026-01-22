"""
Tool Proposals API endpoints.

Provides CRUD operations for LLM-proposed tool definitions that require
owner approval before deployment to n8n.
"""

import os
import httpx
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import desc
import structlog

from app.database import get_db
from app.models import ToolProposal, User, SystemSetting, ExternalAPIKey, ToolRegistry
from app.auth.oidc import get_current_user
from app.utils.encryption import decrypt_value

logger = structlog.get_logger()

# n8n configuration
N8N_URL = os.getenv("N8N_URL", "http://localhost:5678")

router = APIRouter(prefix="/api/tool-proposals", tags=["tool-proposals"])


# =============================================================================
# Pydantic Models
# =============================================================================

class ToolProposalResponse(BaseModel):
    """Response model for tool proposals."""
    id: int
    proposal_id: str
    name: str
    description: str
    trigger_phrases: List[str]
    status: str
    created_by: str
    created_at: datetime
    approved_by_id: Optional[int] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    n8n_workflow_id: Optional[str] = None
    deployed_at: Optional[datetime] = None
    error_message: Optional[str] = None

    class Config:
        from_attributes = True


class ToolProposalCreate(BaseModel):
    """Request model for creating a tool proposal."""
    name: str
    description: str
    trigger_phrases: List[str]
    workflow_definition: dict
    created_by: str = "llm"


class ToolProposalApprove(BaseModel):
    """Request model for approving a proposal."""
    pass  # No additional fields needed


class ToolProposalReject(BaseModel):
    """Request model for rejecting a proposal."""
    reason: Optional[str] = None


# =============================================================================
# List and Get Proposals
# =============================================================================

@router.get("", response_model=List[ToolProposalResponse])
async def list_tool_proposals(
    status: Optional[str] = Query(None, description="Filter by status (pending, approved, rejected, deployed, failed)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    """
    List tool proposals with optional status filter.

    No authentication required for listing (read-only).
    """
    try:
        query = db.query(ToolProposal)

        if status:
            query = query.filter(ToolProposal.status == status)

        proposals = query.order_by(desc(ToolProposal.created_at)).offset(offset).limit(limit).all()

        return [
            ToolProposalResponse(
                id=p.id,
                proposal_id=p.proposal_id,
                name=p.name,
                description=p.description,
                trigger_phrases=p.trigger_phrases or [],
                status=p.status,
                created_by=p.created_by,
                created_at=p.created_at,
                approved_by_id=p.approved_by_id,
                approved_at=p.approved_at,
                rejection_reason=p.rejection_reason,
                n8n_workflow_id=p.n8n_workflow_id,
                deployed_at=p.deployed_at,
                error_message=p.error_message,
            )
            for p in proposals
        ]

    except Exception as e:
        logger.error("tool_proposals_list_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to list proposals: {str(e)}")


@router.get("/{proposal_id}")
async def get_tool_proposal(
    proposal_id: str,
    db: Session = Depends(get_db)
):
    """
    Get a specific tool proposal by proposal_id.

    Returns the full proposal including workflow definition.
    """
    proposal = db.query(ToolProposal).filter(ToolProposal.proposal_id == proposal_id).first()

    if not proposal:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")

    return proposal.to_dict()


# =============================================================================
# Create Proposal
# =============================================================================

@router.post("", response_model=ToolProposalResponse)
async def create_tool_proposal(
    data: ToolProposalCreate,
    db: Session = Depends(get_db)
):
    """
    Create a new tool proposal.

    Called by the LLM when it proposes a new tool.
    If auto-approve is enabled, the proposal is automatically approved.
    """
    import secrets

    try:
        # Check if auto-approve is enabled
        auto_approve_setting = db.query(SystemSetting).filter(
            SystemSetting.key == "tool_proposals_auto_approve"
        ).first()

        auto_approve = False
        if auto_approve_setting:
            auto_approve = auto_approve_setting.value.lower() in ("true", "1", "yes")

        # Generate unique proposal ID
        proposal_id = secrets.token_hex(4)  # 8-character hex string

        # Set initial status based on auto-approve setting
        initial_status = 'approved' if auto_approve else 'pending'

        proposal = ToolProposal(
            proposal_id=proposal_id,
            name=data.name,
            description=data.description,
            trigger_phrases=data.trigger_phrases,
            workflow_definition=data.workflow_definition,
            created_by=data.created_by,
            status=initial_status,
            approved_at=datetime.utcnow() if auto_approve else None,
        )

        db.add(proposal)
        db.commit()
        db.refresh(proposal)

        if auto_approve:
            logger.info("tool_proposal_auto_approved", proposal_id=proposal_id, name=data.name)
        else:
            logger.info("tool_proposal_created", proposal_id=proposal_id, name=data.name)

        return ToolProposalResponse(
            id=proposal.id,
            proposal_id=proposal.proposal_id,
            name=proposal.name,
            description=proposal.description,
            trigger_phrases=proposal.trigger_phrases or [],
            status=proposal.status,
            created_by=proposal.created_by,
            created_at=proposal.created_at,
            approved_at=proposal.approved_at,
        )

    except Exception as e:
        db.rollback()
        logger.error("tool_proposal_create_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to create proposal: {str(e)}")


# =============================================================================
# n8n Deployment Helper
# =============================================================================

async def get_n8n_api_key(db: Session) -> Optional[str]:
    """Get n8n API key from external_api_keys table."""
    try:
        key_record = db.query(ExternalAPIKey).filter(
            ExternalAPIKey.service_name == "n8n",
            ExternalAPIKey.enabled == True
        ).first()

        if key_record and key_record.api_key_encrypted:
            return decrypt_value(key_record.api_key_encrypted)
    except Exception as e:
        logger.warning("n8n_api_key_fetch_failed", error=str(e))

    # Fall back to environment variable
    return os.getenv("N8N_API_KEY")


async def inject_api_keys(workflow_def: dict, db: Session) -> dict:
    """
    Replace API key placeholders with real keys from external_api_keys table.

    Placeholders look like: {{ATHENA_API_KEY:service_name}}
    """
    import re
    import json

    # Convert to string, replace placeholders, convert back
    workflow_str = json.dumps(workflow_def)

    # Find all placeholders
    pattern = r'\{\{ATHENA_API_KEY:(\w+)\}\}'
    matches = re.findall(pattern, workflow_str)

    for service_name in set(matches):
        try:
            key_record = db.query(ExternalAPIKey).filter(
                ExternalAPIKey.service_name == service_name,
                ExternalAPIKey.enabled == True
            ).first()

            if key_record and key_record.api_key_encrypted:
                real_key = decrypt_value(key_record.api_key_encrypted)
                placeholder = f"{{{{ATHENA_API_KEY:{service_name}}}}}"
                workflow_str = workflow_str.replace(placeholder, real_key)
                logger.info("api_key_injected", service=service_name)
            else:
                logger.warning("api_key_not_found", service=service_name)
        except Exception as e:
            logger.error("api_key_injection_failed", service=service_name, error=str(e))

    return json.loads(workflow_str)


async def deploy_workflow_to_n8n(
    proposal: ToolProposal,
    api_key: str,
    db: Session = None
) -> Optional[str]:
    """
    Deploy a workflow to n8n and return the workflow ID.

    Returns:
        Workflow ID if successful, None if failed
    """
    if not proposal.workflow_definition:
        # Create a simple webhook workflow if no definition exists
        workflow_def = {
            "name": f"Athena Tool: {proposal.name}",
            "nodes": [
                {
                    "id": "webhook-trigger",
                    "name": "Webhook",
                    "type": "n8n-nodes-base.webhook",
                    "typeVersion": 1,
                    "position": [250, 300],
                    "parameters": {
                        "path": f"athena-tool-{proposal.name}",
                        "httpMethod": "POST",
                        "responseMode": "lastNode"
                    }
                },
                {
                    "id": "respond-webhook",
                    "name": "Respond to Webhook",
                    "type": "n8n-nodes-base.respondToWebhook",
                    "typeVersion": 1,
                    "position": [450, 300],
                    "parameters": {
                        "respondWith": "text",
                        "responseBody": f"Tool '{proposal.name}' executed successfully. This is a placeholder - implement your logic here."
                    }
                }
            ],
            "connections": {
                "Webhook": {
                    "main": [[{"node": "Respond to Webhook", "type": "main", "index": 0}]]
                }
            },
            "settings": {"executionOrder": "v1"}
        }
    else:
        import copy
        workflow_def = copy.deepcopy(proposal.workflow_definition)
        # Ensure required fields
        if "name" not in workflow_def:
            workflow_def["name"] = f"Athena Tool: {proposal.name}"
        # Remove read-only fields that n8n rejects
        workflow_def.pop("active", None)
        workflow_def.pop("tags", None)
        workflow_def.pop("_athena_metadata", None)  # Internal metadata

    # Add webhookId to webhook nodes if missing
    for node in workflow_def.get("nodes", []):
        if node.get("type") == "n8n-nodes-base.webhook" and "webhookId" not in node:
            node["webhookId"] = f"athena-{proposal.name}"

    # Inject API keys from external_api_keys table
    if db:
        workflow_def = await inject_api_keys(workflow_def, db)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Create workflow
            response = await client.post(
                f"{N8N_URL}/api/v1/workflows",
                headers={
                    "x-n8n-api-key": api_key,  # lowercase required by n8n API
                    "Content-Type": "application/json"
                },
                json=workflow_def
            )

            if response.status_code not in (200, 201):
                logger.error("n8n_workflow_creation_failed",
                           status=response.status_code,
                           response=response.text[:500])
                return None

            data = response.json()
            workflow_id = data.get("id")

            logger.info("n8n_workflow_created",
                       workflow_id=workflow_id,
                       name=proposal.name)

            # Optionally activate the workflow
            # await client.patch(
            #     f"{N8N_URL}/api/v1/workflows/{workflow_id}",
            #     headers={"X-N8N-API-KEY": api_key},
            #     json={"active": True}
            # )

            return workflow_id

    except Exception as e:
        logger.error("n8n_deployment_error", error=str(e))
        return None


# =============================================================================
# Tool Registry Integration
# =============================================================================

async def register_self_built_tool(proposal: ToolProposal, db: Session) -> bool:
    """
    Register a deployed tool in the tool_registry for discoverability.

    This makes the tool available for LLM tool calling immediately after
    n8n deployment, without requiring a manual registry entry.

    Args:
        proposal: The deployed ToolProposal
        db: Database session

    Returns:
        True if registration succeeded, False otherwise
    """
    # Build webhook URL following n8n pattern
    webhook_url = f"{N8N_URL}/webhook/athena-tool-{proposal.name}"

    # Build function schema for OpenAI tool calling format
    # Extract parameters from workflow_definition if available
    parameters = {"type": "object", "properties": {}, "required": []}
    if proposal.workflow_definition:
        # Check for _athena_metadata with parameter definitions
        metadata = proposal.workflow_definition.get("_athena_metadata", {})
        if "parameters" in metadata:
            parameters = metadata["parameters"]
        # Or check for a parameters key directly
        elif "parameters" in proposal.workflow_definition:
            parameters = proposal.workflow_definition["parameters"]

    function_schema = {
        "type": "function",
        "function": {
            "name": proposal.name,
            "description": proposal.description or f"Self-built tool: {proposal.name}",
            "parameters": parameters
        }
    }

    # Check if tool already exists in registry
    existing_tool = db.query(ToolRegistry).filter(
        ToolRegistry.tool_name == proposal.name
    ).first()

    if existing_tool:
        # Update existing entry
        existing_tool.enabled = True
        existing_tool.service_url = webhook_url
        existing_tool.function_schema = function_schema
        existing_tool.source = 'self_built'
        existing_tool.description = proposal.description or existing_tool.description
        existing_tool.mcp_endpoint = webhook_url  # Store webhook URL here too
        existing_tool.discovery_metadata = {
            "proposal_id": proposal.proposal_id,
            "n8n_workflow_id": proposal.n8n_workflow_id,
            "trigger_phrases": proposal.trigger_phrases or [],
            "deployed_at": proposal.deployed_at.isoformat() if proposal.deployed_at else None
        }
        logger.info("tool_registry_updated",
                   tool_name=proposal.name,
                   source="self_built")
    else:
        # Create new registry entry
        new_tool = ToolRegistry(
            tool_name=proposal.name,
            display_name=proposal.description[:100] if proposal.description else proposal.name,
            description=proposal.description or f"Self-built tool: {proposal.name}",
            category='dynamic',  # Self-built tools are dynamic
            function_schema=function_schema,
            enabled=True,
            guest_mode_allowed=False,  # Default to owner-only for security
            service_url=webhook_url,
            source='self_built',
            priority=40,  # Between static (10) and MCP (50)
            timeout_seconds=30,
            mcp_endpoint=webhook_url,
            discovery_metadata={
                "proposal_id": proposal.proposal_id,
                "n8n_workflow_id": proposal.n8n_workflow_id,
                "trigger_phrases": proposal.trigger_phrases or [],
                "deployed_at": proposal.deployed_at.isoformat() if proposal.deployed_at else None
            }
        )
        db.add(new_tool)
        logger.info("tool_registry_created",
                   tool_name=proposal.name,
                   source="self_built",
                   webhook_url=webhook_url)

    db.commit()

    # Notify orchestrator to refresh tool cache
    await notify_orchestrator_tool_refresh()

    return True


async def notify_orchestrator_tool_refresh():
    """
    Notify orchestrator to refresh its tool cache.

    Non-fatal if this fails - orchestrator will refresh on next cache expiry.
    """
    orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8001")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(f"{orchestrator_url}/tools/refresh")
            if response.status_code == 200:
                logger.info("orchestrator_tool_cache_refreshed")
            else:
                logger.warning("orchestrator_refresh_failed",
                             status=response.status_code)
    except Exception as e:
        logger.warning("orchestrator_refresh_error", error=str(e))
        # Non-fatal - orchestrator will refresh on cache expiry


async def cleanup_self_built_tool(tool_name: str, db: Session):
    """
    Remove a self-built tool from the registry.

    Called when a tool proposal is deleted or undone.
    """
    tool_entry = db.query(ToolRegistry).filter(
        ToolRegistry.tool_name == tool_name,
        ToolRegistry.source == 'self_built'
    ).first()

    if tool_entry:
        db.delete(tool_entry)
        db.commit()
        logger.info("self_built_tool_removed", tool_name=tool_name)

        # Notify orchestrator
        await notify_orchestrator_tool_refresh()


# =============================================================================
# Approve / Reject Proposals
# =============================================================================

@router.post("/{proposal_id}/approve")
async def approve_tool_proposal(
    proposal_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Approve a tool proposal and deploy to n8n.

    Requires authentication.
    """
    proposal = db.query(ToolProposal).filter(ToolProposal.proposal_id == proposal_id).first()

    if not proposal:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")

    if proposal.status != 'pending':
        raise HTTPException(status_code=400, detail=f"Proposal is not pending (status: {proposal.status})")

    try:
        # Get n8n API key
        n8n_api_key = await get_n8n_api_key(db)

        workflow_id = None
        deployment_error = None

        if n8n_api_key:
            # Deploy to n8n (pass db for API key injection)
            workflow_id = await deploy_workflow_to_n8n(proposal, n8n_api_key, db)
            if not workflow_id:
                deployment_error = "Failed to create n8n workflow"
        else:
            deployment_error = "n8n API key not configured"
            logger.warning("n8n_api_key_not_configured")

        # Update proposal status
        proposal.status = 'approved' if workflow_id else 'approved'  # Still approve even if deployment fails
        proposal.approved_by_id = current_user.id
        proposal.approved_at = datetime.utcnow()

        if workflow_id:
            proposal.n8n_workflow_id = workflow_id
            proposal.deployed_at = datetime.utcnow()
            proposal.status = 'deployed'
        elif deployment_error:
            proposal.error_message = deployment_error

        db.commit()
        db.refresh(proposal)

        # If deployment succeeded, register tool in tool_registry for discoverability
        tool_registered = False
        if workflow_id:
            try:
                tool_registered = await register_self_built_tool(proposal, db)
            except Exception as e:
                logger.warning("tool_registry_registration_failed",
                             proposal_id=proposal_id, error=str(e))

        logger.info("tool_proposal_approved",
                   proposal_id=proposal_id,
                   approved_by=current_user.username,
                   workflow_id=workflow_id,
                   deployed=bool(workflow_id),
                   tool_registered=tool_registered)

        return {
            "status": proposal.status,
            "proposal_id": proposal_id,
            "n8n_workflow_id": workflow_id,
            "tool_registered": tool_registered,
            "message": "Proposal approved and deployed to n8n" if workflow_id else f"Proposal approved but deployment failed: {deployment_error}"
        }

    except Exception as e:
        db.rollback()
        logger.error("tool_proposal_approve_error", error=str(e), proposal_id=proposal_id)
        raise HTTPException(status_code=500, detail=f"Failed to approve proposal: {str(e)}")


@router.post("/{proposal_id}/reject")
async def reject_tool_proposal(
    proposal_id: str,
    data: ToolProposalReject,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Reject a tool proposal.

    Requires authentication.
    """
    proposal = db.query(ToolProposal).filter(ToolProposal.proposal_id == proposal_id).first()

    if not proposal:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")

    if proposal.status != 'pending':
        raise HTTPException(status_code=400, detail=f"Proposal is not pending (status: {proposal.status})")

    try:
        proposal.status = 'rejected'
        proposal.approved_by_id = current_user.id
        proposal.approved_at = datetime.utcnow()
        proposal.rejection_reason = data.reason

        db.commit()

        logger.info("tool_proposal_rejected",
                   proposal_id=proposal_id,
                   rejected_by=current_user.username,
                   reason=data.reason)

        return {
            "status": "rejected",
            "proposal_id": proposal_id,
            "message": "Proposal rejected"
        }

    except Exception as e:
        db.rollback()
        logger.error("tool_proposal_reject_error", error=str(e), proposal_id=proposal_id)
        raise HTTPException(status_code=500, detail=f"Failed to reject proposal: {str(e)}")


# =============================================================================
# Delete Proposal
# =============================================================================

@router.delete("/{proposal_id}")
async def delete_tool_proposal(
    proposal_id: str,
    force: bool = Query(False, description="Force delete deployed proposals"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Delete a tool proposal.

    Requires authentication. Deployed proposals require force=True to delete.
    When deleting a deployed proposal, the tool registry entry is also removed.
    """
    proposal = db.query(ToolProposal).filter(ToolProposal.proposal_id == proposal_id).first()

    if not proposal:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")

    if proposal.status == 'deployed' and not force:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a deployed proposal without force=True. "
                   "This will also remove the tool from the registry."
        )

    try:
        tool_name = proposal.name
        was_deployed = proposal.status == 'deployed'

        # Delete the proposal
        db.delete(proposal)
        db.commit()

        # If it was deployed, clean up tool registry
        tool_cleaned = False
        if was_deployed:
            try:
                await cleanup_self_built_tool(tool_name, db)
                tool_cleaned = True
            except Exception as e:
                logger.warning("tool_cleanup_failed", tool_name=tool_name, error=str(e))

        logger.info("tool_proposal_deleted",
                   proposal_id=proposal_id,
                   deleted_by=current_user.username,
                   was_deployed=was_deployed,
                   tool_cleaned=tool_cleaned)

        return {
            "status": "deleted",
            "proposal_id": proposal_id,
            "tool_registry_cleaned": tool_cleaned if was_deployed else None
        }

    except Exception as e:
        db.rollback()
        logger.error("tool_proposal_delete_error", error=str(e), proposal_id=proposal_id)
        raise HTTPException(status_code=500, detail=f"Failed to delete proposal: {str(e)}")


# =============================================================================
# Stats
# =============================================================================

@router.get("/stats/summary")
async def get_tool_proposal_stats(db: Session = Depends(get_db)):
    """Get summary statistics for tool proposals."""
    from sqlalchemy import func

    try:
        stats = db.query(
            ToolProposal.status,
            func.count(ToolProposal.id)
        ).group_by(ToolProposal.status).all()

        status_counts = {status: count for status, count in stats}

        return {
            "pending": status_counts.get('pending', 0),
            "approved": status_counts.get('approved', 0),
            "rejected": status_counts.get('rejected', 0),
            "deployed": status_counts.get('deployed', 0),
            "failed": status_counts.get('failed', 0),
            "total": sum(status_counts.values()),
        }

    except Exception as e:
        logger.error("tool_proposal_stats_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {str(e)}")
