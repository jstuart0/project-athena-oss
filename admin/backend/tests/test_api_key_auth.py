"""
Integration tests for API key authentication on protected endpoints.
"""
import os
import pytest
from datetime import datetime, timedelta

# Set test environment
os.environ["DEV_MODE"] = "true"


class TestApiKeyAuthentication:
    """Tests for authenticating with API keys."""

    def test_valid_key_authenticates(self, client):
        """Valid API key should authenticate successfully."""
        # Create key
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Auth Test', 'scopes': ['read:*']}
        )
        api_key = create_response.json()['api_key']

        # Use key to access protected endpoint
        response = client.get(
            '/api/devices',
            headers={'X-API-Key': api_key}
        )
        assert response.status_code == 200

    def test_invalid_key_returns_401(self, client):
        """Invalid API key should return 401."""
        response = client.get(
            '/api/devices',
            headers={'X-API-Key': 'athena_1234567890_invalidkey'}
        )
        assert response.status_code == 401

    def test_malformed_key_returns_401(self, client):
        """Malformed key should return 401."""
        response = client.get(
            '/api/devices',
            headers={'X-API-Key': 'not_a_valid_format'}
        )
        assert response.status_code == 401

    def test_empty_key_returns_401(self, client):
        """Empty API key header should return 401."""
        response = client.get(
            '/api/devices',
            headers={'X-API-Key': ''}
        )
        assert response.status_code == 401

    def test_revoked_key_returns_401(self, client):
        """Revoked key should return 401."""
        # Create and revoke key
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Revoke Test', 'scopes': ['read:*']}
        )
        api_key = create_response.json()['api_key']
        key_id = create_response.json()['id']

        client.delete(f'/api/user-api-keys/{key_id}')

        # Try to use revoked key
        response = client.get(
            '/api/devices',
            headers={'X-API-Key': api_key}
        )
        assert response.status_code == 401
        assert 'revoked' in response.json()['detail'].lower()

    def test_expired_key_returns_401(self, client, db, test_user):
        """Expired key should return 401."""
        from app.utils.api_keys import generate_api_key, hash_api_key, extract_key_prefix
        from app.models import UserAPIKey

        # Create expired key directly in DB
        raw_key = generate_api_key()
        key = UserAPIKey(
            user_id=test_user.id,
            name="Expired Test",
            key_prefix=extract_key_prefix(raw_key),
            key_hash=hash_api_key(raw_key),
            scopes=["read:*"],
            expires_at=datetime.utcnow() - timedelta(hours=1),
            created_by_id=test_user.id,
        )
        db.add(key)
        db.commit()

        # Try to use expired key
        response = client.get(
            '/api/devices',
            headers={'X-API-Key': raw_key}
        )
        assert response.status_code == 401
        assert 'expired' in response.json()['detail'].lower()

    def test_key_for_inactive_user_returns_401(self, client, db, test_user):
        """Key for deactivated user should return 401."""
        from app.utils.api_keys import generate_api_key, hash_api_key, extract_key_prefix
        from app.models import UserAPIKey

        # Create key for user
        raw_key = generate_api_key()
        key = UserAPIKey(
            user_id=test_user.id,
            name="Inactive User Test",
            key_prefix=extract_key_prefix(raw_key),
            key_hash=hash_api_key(raw_key),
            scopes=["read:*"],
            created_by_id=test_user.id,
        )
        db.add(key)
        db.commit()

        # Deactivate user
        test_user.active = False
        db.commit()

        # Try to use key
        response = client.get(
            '/api/devices',
            headers={'X-API-Key': raw_key}
        )
        assert response.status_code == 401
        assert 'inactive' in response.json()['detail'].lower()


class TestApiKeyUsageTracking:
    """Tests for API key usage tracking."""

    def test_last_used_updated(self, client):
        """last_used_at should be updated on use."""
        # Create key
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Usage Test', 'scopes': ['read:*']}
        )
        api_key = create_response.json()['api_key']
        key_id = create_response.json()['id']

        # Use key
        client.get('/api/devices', headers={'X-API-Key': api_key})

        # Check last_used updated
        get_response = client.get(f'/api/user-api-keys/{key_id}')
        assert get_response.json()['last_used_at'] is not None

    def test_request_count_incremented(self, client):
        """request_count should increment on each use."""
        # Create key
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Count Test', 'scopes': ['read:*']}
        )
        api_key = create_response.json()['api_key']
        key_id = create_response.json()['id']

        # Use key multiple times
        for _ in range(5):
            client.get('/api/devices', headers={'X-API-Key': api_key})

        # Check request count
        get_response = client.get(f'/api/user-api-keys/{key_id}')
        assert get_response.json()['request_count'] == 5


class TestMultipleAuthMethods:
    """Tests for both JWT and API key auth on same endpoints."""

    def test_jwt_still_works(self, client):
        """JWT authentication should still work (DEV_MODE)."""
        # In DEV_MODE, requests without auth headers get dev-admin
        response = client.get('/api/devices')
        assert response.status_code == 200

    def test_api_key_works_on_same_endpoint(self, client):
        """API key should work on endpoints that also accept JWT."""
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Multi Auth Test', 'scopes': ['read:*']}
        )
        api_key = create_response.json()['api_key']

        response = client.get(
            '/api/devices',
            headers={'X-API-Key': api_key}
        )
        assert response.status_code == 200

    def test_api_key_takes_precedence(self, client):
        """If both headers provided, API key should be checked."""
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Precedence Test', 'scopes': ['read:*']}
        )
        api_key = create_response.json()['api_key']

        # Provide both headers (API key valid, JWT invalid)
        response = client.get(
            '/api/devices',
            headers={
                'X-API-Key': api_key,
                'Authorization': 'Bearer invalid_jwt_token'
            }
        )
        # Should succeed because API key is checked first and is valid
        assert response.status_code == 200

    def test_invalid_api_key_doesnt_fallback_to_jwt(self, client):
        """Invalid API key should not fall back to JWT."""
        response = client.get(
            '/api/devices',
            headers={
                'X-API-Key': 'athena_0000_invalid',
                'Authorization': 'Bearer some_token'  # Would work in DEV_MODE
            }
        )
        # Should fail because invalid API key doesn't fall through
        assert response.status_code == 401
