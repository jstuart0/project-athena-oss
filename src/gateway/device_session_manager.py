"""
Device Session Manager for Voice PE Units.

Manages conversation sessions for Home Assistant Voice PE devices,
mapping each device to an active session_id to maintain conversation context.
"""

import asyncio
from typing import Dict, Optional
from datetime import datetime, timedelta
import structlog

logger = structlog.get_logger()


class DeviceSessionManager:
    """
    Manages conversation sessions for Voice PE devices.

    Each Voice PE device (identified by device_id/zone) gets a persistent
    session that maintains conversation context across multiple interactions.

    Sessions automatically expire after a period of inactivity (timeout).
    """

    def __init__(
        self,
        session_timeout: int = 300,  # 5 minutes default
        max_session_age: int = 86400  # 24 hours default
    ):
        """
        Initialize Device Session Manager.

        Args:
            session_timeout: Seconds of inactivity before session expires (default 300 = 5 min)
            max_session_age: Maximum age of a session in seconds (default 86400 = 24 hours)
        """
        self.device_sessions: Dict[str, Dict] = {}
        self.session_timeout = session_timeout
        self.max_session_age = max_session_age
        self._cleanup_task = None

        logger.info(
            "device_session_manager_initialized",
            session_timeout=session_timeout,
            max_session_age=max_session_age
        )

    async def initialize(self):
        """Start background cleanup task."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("device_session_cleanup_started")

    async def close(self):
        """Stop background cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("device_session_cleanup_stopped")

    async def get_session_for_device(
        self,
        device_id: str,
        force_new: bool = False
    ) -> Optional[str]:
        """
        Get active session_id for a device, or None to create new.

        Args:
            device_id: Voice PE device identifier (e.g., "office", "kitchen", "bedroom_1")
            force_new: Force creation of new session (e.g., on explicit wake word)

        Returns:
            session_id if active session exists and not expired, None to create new
        """
        if force_new:
            # Clear existing session
            if device_id in self.device_sessions:
                old_session_id = self.device_sessions[device_id]["session_id"]
                del self.device_sessions[device_id]
                logger.info(
                    "device_session_forced_new",
                    device_id=device_id,
                    old_session_id=old_session_id
                )
            return None

        if device_id not in self.device_sessions:
            logger.debug("device_session_not_found", device_id=device_id)
            return None

        session_info = self.device_sessions[device_id]
        last_activity = session_info["last_activity"]
        created_at = session_info["created_at"]

        # Check if session expired due to inactivity
        time_since_activity = (datetime.utcnow() - last_activity).total_seconds()
        if time_since_activity > self.session_timeout:
            session_id = session_info["session_id"]
            del self.device_sessions[device_id]
            logger.info(
                "device_session_expired_timeout",
                device_id=device_id,
                session_id=session_id,
                seconds_inactive=time_since_activity
            )
            return None

        # Check if session exceeded max age
        session_age = (datetime.utcnow() - created_at).total_seconds()
        if session_age > self.max_session_age:
            session_id = session_info["session_id"]
            del self.device_sessions[device_id]
            logger.info(
                "device_session_expired_age",
                device_id=device_id,
                session_id=session_id,
                age_seconds=session_age
            )
            return None

        session_id = session_info["session_id"]
        logger.debug(
            "device_session_found",
            device_id=device_id,
            session_id=session_id,
            age_seconds=session_age,
            inactive_seconds=time_since_activity
        )
        return session_id

    async def update_session_for_device(
        self,
        device_id: str,
        session_id: str
    ):
        """
        Update session mapping after orchestrator call.

        Args:
            device_id: Voice PE device identifier
            session_id: Session ID returned from orchestrator
        """
        is_new = device_id not in self.device_sessions

        if is_new:
            # New session for this device
            self.device_sessions[device_id] = {
                "session_id": session_id,
                "device_id": device_id,
                "created_at": datetime.utcnow(),
                "last_activity": datetime.utcnow(),
                "interaction_count": 1
            }
            logger.info(
                "device_session_created",
                device_id=device_id,
                session_id=session_id
            )
        else:
            # Update existing session
            self.device_sessions[device_id]["session_id"] = session_id
            self.device_sessions[device_id]["last_activity"] = datetime.utcnow()
            self.device_sessions[device_id]["interaction_count"] += 1

            logger.debug(
                "device_session_updated",
                device_id=device_id,
                session_id=session_id,
                interaction_count=self.device_sessions[device_id]["interaction_count"]
            )

    async def get_all_active_sessions(self) -> Dict[str, Dict]:
        """
        Get all currently active device sessions.

        Returns:
            Dictionary mapping device_id to session info
        """
        return self.device_sessions.copy()

    async def clear_session_for_device(self, device_id: str) -> bool:
        """
        Manually clear session for a specific device.

        Args:
            device_id: Device to clear session for

        Returns:
            True if session was cleared, False if no session existed
        """
        if device_id in self.device_sessions:
            session_id = self.device_sessions[device_id]["session_id"]
            del self.device_sessions[device_id]
            logger.info(
                "device_session_cleared",
                device_id=device_id,
                session_id=session_id
            )
            return True
        return False

    async def get_session_info(self, device_id: str) -> Optional[Dict]:
        """
        Get detailed session information for a device.

        Args:
            device_id: Device to query

        Returns:
            Session info dict or None if no active session
        """
        return self.device_sessions.get(device_id)

    async def _cleanup_loop(self):
        """Background task to cleanup expired sessions."""
        while True:
            try:
                await asyncio.sleep(60)  # Run every minute

                expired_devices = []
                now = datetime.utcnow()

                for device_id, session_info in list(self.device_sessions.items()):
                    last_activity = session_info["last_activity"]
                    created_at = session_info["created_at"]

                    # Check timeout
                    time_since_activity = (now - last_activity).total_seconds()
                    if time_since_activity > self.session_timeout:
                        expired_devices.append((device_id, "timeout", time_since_activity))
                        continue

                    # Check max age
                    session_age = (now - created_at).total_seconds()
                    if session_age > self.max_session_age:
                        expired_devices.append((device_id, "max_age", session_age))

                # Remove expired sessions
                for device_id, reason, duration in expired_devices:
                    session_id = self.device_sessions[device_id]["session_id"]
                    del self.device_sessions[device_id]
                    logger.info(
                        "device_session_cleaned_up",
                        device_id=device_id,
                        session_id=session_id,
                        reason=reason,
                        duration_seconds=duration
                    )

                if expired_devices:
                    logger.info(
                        "device_session_cleanup_completed",
                        cleaned=len(expired_devices),
                        remaining=len(self.device_sessions)
                    )

            except asyncio.CancelledError:
                logger.info("device_session_cleanup_cancelled")
                raise
            except Exception as e:
                logger.error("device_session_cleanup_error", error=str(e), exc_info=True)


# Global instance
_device_session_manager: Optional[DeviceSessionManager] = None


async def get_device_session_manager() -> DeviceSessionManager:
    """
    Get global device session manager instance.

    Returns:
        DeviceSessionManager instance
    """
    global _device_session_manager
    if _device_session_manager is None:
        _device_session_manager = DeviceSessionManager()
        await _device_session_manager.initialize()
    return _device_session_manager
