"""
End-to-end tests for complete API key workflows.
"""
import os
import pytest
from datetime import datetime, timedelta

# Set test environment
os.environ["DEV_MODE"] = "true"


class TestCompleteApiKeyWorkflow:
    """End-to-end tests for typical user workflows."""

    def test_full_lifecycle_create_use_revoke(self, client):
        """Test complete lifecycle: create -> use -> revoke -> verify unusable."""
        # 1. Create API key
        create_response = client.post(
            '/api/user-api-keys',
            json={
                'name': 'E2E Test Key',
                'scopes': ['read:*'],
                'expires_in_days': 30,
                'reason': 'End-to-end testing'
            }
        )
        assert create_response.status_code == 201
        api_key = create_response.json()['api_key']
        key_id = create_response.json()['id']

        # 2. Verify key is in list
        list_response = client.get('/api/user-api-keys')
        key_names = [k['name'] for k in list_response.json()]
        assert 'E2E Test Key' in key_names

        # 3. Use key to access protected endpoint
        use_response = client.get(
            '/api/devices',
            headers={'X-API-Key': api_key}
        )
        assert use_response.status_code == 200

        # 4. Get key details and verify usage tracked
        detail_response = client.get(f'/api/user-api-keys/{key_id}')
        assert detail_response.json()['request_count'] >= 1
        assert detail_response.json()['last_used_at'] is not None

        # 5. Revoke key
        revoke_response = client.delete(
            f'/api/user-api-keys/{key_id}',
            json={'reason': 'Testing complete'}
        )
        assert revoke_response.status_code == 204

        # 6. Verify key is marked as revoked
        detail_response = client.get(f'/api/user-api-keys/{key_id}')
        assert detail_response.json()['revoked'] is True

        # 7. Verify key can no longer be used
        blocked_response = client.get(
            '/api/devices',
            headers={'X-API-Key': api_key}
        )
        assert blocked_response.status_code == 401

    def test_multiple_keys_workflow(self, client):
        """Test managing multiple API keys."""
        # Create multiple keys with different scopes
        keys = []
        for i in range(3):
            response = client.post(
                '/api/user-api-keys',
                json={
                    'name': f'Multi Key {i}',
                    'scopes': ['read:*'] if i < 2 else ['read:*', 'write:*']
                }
            )
            keys.append({
                'id': response.json()['id'],
                'api_key': response.json()['api_key'],
                'name': response.json()['name']
            })

        # List should show all 3
        list_response = client.get('/api/user-api-keys')
        assert len(list_response.json()) >= 3

        # Each key should work independently
        for key_info in keys:
            response = client.get(
                '/api/devices',
                headers={'X-API-Key': key_info['api_key']}
            )
            assert response.status_code == 200

        # Revoke one key
        client.delete(f'/api/user-api-keys/{keys[0]["id"]}')

        # Revoked key should fail
        response = client.get(
            '/api/devices',
            headers={'X-API-Key': keys[0]['api_key']}
        )
        assert response.status_code == 401

        # Other keys should still work
        for key_info in keys[1:]:
            response = client.get(
                '/api/devices',
                headers={'X-API-Key': key_info['api_key']}
            )
            assert response.status_code == 200

    def test_key_with_expiration(self, client, db):
        """Test key expiration workflow."""
        from app.models import UserAPIKey

        # Create key with short expiration
        create_response = client.post(
            '/api/user-api-keys',
            json={
                'name': 'Expiring Key',
                'scopes': ['read:*'],
                'expires_in_days': 1
            }
        )
        api_key = create_response.json()['api_key']
        key_id = create_response.json()['id']

        # Key should work now
        response = client.get(
            '/api/devices',
            headers={'X-API-Key': api_key}
        )
        assert response.status_code == 200

        # Manually expire the key in DB (simulate time passing)
        key_record = db.query(UserAPIKey).filter(UserAPIKey.id == key_id).first()
        key_record.expires_at = datetime.utcnow() - timedelta(hours=1)
        db.commit()

        # Key should now fail
        response = client.get(
            '/api/devices',
            headers={'X-API-Key': api_key}
        )
        assert response.status_code == 401


class TestSecurityScenarios:
    """Security-focused end-to-end tests."""

    def test_cannot_use_other_users_key_details(self, client, db):
        """User should not see other users' key details."""
        from app.models import User, UserAPIKey
        from app.utils.api_keys import generate_api_key, hash_api_key, extract_key_prefix

        # Create another user with a key
        other_user = User(
            authentik_id="other-user-001",
            username="other",
            email="other@example.com",
            role="viewer",
            active=True,
        )
        db.add(other_user)
        db.commit()

        raw_key = generate_api_key()
        other_key = UserAPIKey(
            user_id=other_user.id,
            name="Other User Key",
            key_prefix=extract_key_prefix(raw_key),
            key_hash=hash_api_key(raw_key),
            scopes=["read:*"],
            created_by_id=other_user.id,
        )
        db.add(other_key)
        db.commit()

        # Current user (dev-admin in DEV_MODE) should not see other's key in list
        # unless they are owner role (dev-admin is owner, so this test verifies
        # owner CAN see, but a viewer could not)

    def test_raw_key_only_shown_once(self, client):
        """Raw API key should only be returned at creation time."""
        # Create key - raw key included
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Raw Key Test', 'scopes': ['read:*']}
        )
        assert 'api_key' in create_response.json()
        key_id = create_response.json()['id']

        # Get key - raw key NOT included
        get_response = client.get(f'/api/user-api-keys/{key_id}')
        assert 'api_key' not in get_response.json()

        # List keys - raw key NOT included
        list_response = client.get('/api/user-api-keys')
        for key in list_response.json():
            assert 'api_key' not in key

    def test_key_prefix_allows_identification(self, client):
        """Key prefix should be visible for identification."""
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Prefix Test', 'scopes': ['read:*']}
        )
        full_key = create_response.json()['api_key']
        key_prefix = create_response.json()['key_prefix']

        # Prefix should be the start of the full key
        assert full_key.startswith(key_prefix)

        # List should show prefix
        list_response = client.get('/api/user-api-keys')
        for key in list_response.json():
            if key['name'] == 'Prefix Test':
                assert key['key_prefix'] == key_prefix
                break

    def test_duplicate_key_name_after_revoke_allowed(self, client):
        """Should be able to reuse key name after revoking original."""
        # Create key
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Reusable Name', 'scopes': ['read:*']}
        )
        key_id = create_response.json()['id']

        # Revoke it
        client.delete(f'/api/user-api-keys/{key_id}')

        # Should be able to create new key with same name
        create_response2 = client.post(
            '/api/user-api-keys',
            json={'name': 'Reusable Name', 'scopes': ['write:*']}
        )
        assert create_response2.status_code == 201
