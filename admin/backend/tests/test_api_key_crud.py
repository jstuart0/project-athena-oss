"""
Integration tests for API key CRUD operations.
"""
import os
import pytest

# Set test environment
os.environ["DEV_MODE"] = "true"


class TestCreateApiKey:
    """Tests for POST /api/user-api-keys."""

    def test_create_key_returns_201(self, client):
        """Creating a key should return 201."""
        response = client.post(
            '/api/user-api-keys',
            json={'name': 'Test Key', 'scopes': ['read:*']}
        )
        assert response.status_code == 201

    def test_create_key_returns_raw_key(self, client):
        """Response should include raw API key."""
        response = client.post(
            '/api/user-api-keys',
            json={'name': 'Test Key', 'scopes': ['read:*']}
        )
        data = response.json()
        assert 'api_key' in data
        assert data['api_key'].startswith('athena_')

    def test_create_key_returns_metadata(self, client):
        """Response should include key metadata."""
        response = client.post(
            '/api/user-api-keys',
            json={
                'name': 'My Test Key',
                'scopes': ['read:devices', 'write:features'],
                'expires_in_days': 30,
                'reason': 'Testing'
            }
        )
        data = response.json()
        assert data['name'] == 'My Test Key'
        assert data['scopes'] == ['read:devices', 'write:features']
        assert data['expires_at'] is not None
        assert data['revoked'] is False

    def test_create_key_requires_name(self, client):
        """Name is required."""
        response = client.post(
            '/api/user-api-keys',
            json={'scopes': ['read:*']}
        )
        assert response.status_code == 422

    def test_create_key_requires_scopes(self, client):
        """Scopes are required."""
        response = client.post(
            '/api/user-api-keys',
            json={'name': 'Test Key'}
        )
        assert response.status_code == 422

    def test_create_key_requires_at_least_one_scope(self, client):
        """At least one scope is required."""
        response = client.post(
            '/api/user-api-keys',
            json={'name': 'Test Key', 'scopes': []}
        )
        assert response.status_code == 422

    def test_duplicate_name_rejected(self, client):
        """Cannot create two active keys with same name."""
        client.post(
            '/api/user-api-keys',
            json={'name': 'Duplicate', 'scopes': ['read:*']}
        )
        response = client.post(
            '/api/user-api-keys',
            json={'name': 'Duplicate', 'scopes': ['read:*']}
        )
        assert response.status_code == 400
        assert 'already have' in response.json()['detail']

    def test_expires_in_days_validated(self, client):
        """Expiration days must be 1-365."""
        response = client.post(
            '/api/user-api-keys',
            json={'name': 'Test', 'scopes': ['read:*'], 'expires_in_days': 0}
        )
        assert response.status_code == 422

        response = client.post(
            '/api/user-api-keys',
            json={'name': 'Test', 'scopes': ['read:*'], 'expires_in_days': 366}
        )
        assert response.status_code == 422


class TestListApiKeys:
    """Tests for GET /api/user-api-keys."""

    def test_list_returns_200(self, client):
        """Listing keys should return 200."""
        response = client.get('/api/user-api-keys')
        assert response.status_code == 200

    def test_list_returns_array(self, client):
        """Response should be an array."""
        response = client.get('/api/user-api-keys')
        assert isinstance(response.json(), list)

    def test_list_includes_created_keys(self, client):
        """Created keys should appear in list."""
        client.post(
            '/api/user-api-keys',
            json={'name': 'Key 1', 'scopes': ['read:*']}
        )
        client.post(
            '/api/user-api-keys',
            json={'name': 'Key 2', 'scopes': ['write:*']}
        )

        response = client.get('/api/user-api-keys')
        keys = response.json()
        names = [k['name'] for k in keys]
        assert 'Key 1' in names
        assert 'Key 2' in names

    def test_list_does_not_include_raw_key(self, client):
        """List should NOT include raw API keys."""
        client.post(
            '/api/user-api-keys',
            json={'name': 'Test', 'scopes': ['read:*']}
        )

        response = client.get('/api/user-api-keys')
        for key in response.json():
            assert 'api_key' not in key

    def test_list_includes_prefix(self, client):
        """List should include key prefix for identification."""
        client.post(
            '/api/user-api-keys',
            json={'name': 'Test', 'scopes': ['read:*']}
        )

        response = client.get('/api/user-api-keys')
        for key in response.json():
            assert 'key_prefix' in key
            assert key['key_prefix'].startswith('athena_')


class TestGetApiKey:
    """Tests for GET /api/user-api-keys/{key_id}."""

    def test_get_existing_key(self, client):
        """Should return key details by ID."""
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Test', 'scopes': ['read:*']}
        )
        key_id = create_response.json()['id']

        response = client.get(f'/api/user-api-keys/{key_id}')
        assert response.status_code == 200
        assert response.json()['name'] == 'Test'

    def test_get_nonexistent_key_404(self, client):
        """Should return 404 for non-existent key."""
        response = client.get('/api/user-api-keys/99999')
        assert response.status_code == 404

    def test_get_does_not_include_raw_key(self, client):
        """Get should NOT include raw API key."""
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Test', 'scopes': ['read:*']}
        )
        key_id = create_response.json()['id']

        response = client.get(f'/api/user-api-keys/{key_id}')
        assert 'api_key' not in response.json()


class TestRevokeApiKey:
    """Tests for DELETE /api/user-api-keys/{key_id}."""

    def test_revoke_returns_204(self, client):
        """Revoking should return 204 No Content."""
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Test', 'scopes': ['read:*']}
        )
        key_id = create_response.json()['id']

        response = client.delete(f'/api/user-api-keys/{key_id}')
        assert response.status_code == 204

    def test_revoke_marks_as_revoked(self, client):
        """Revoked key should be marked as revoked."""
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Test', 'scopes': ['read:*']}
        )
        key_id = create_response.json()['id']

        client.delete(f'/api/user-api-keys/{key_id}')

        get_response = client.get(f'/api/user-api-keys/{key_id}')
        assert get_response.json()['revoked'] is True
        assert get_response.json()['revoked_at'] is not None

    def test_revoke_with_reason(self, client):
        """Should store revocation reason."""
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Test', 'scopes': ['read:*']}
        )
        key_id = create_response.json()['id']

        client.delete(
            f'/api/user-api-keys/{key_id}',
            json={'reason': 'No longer needed'}
        )
        # Reason stored in database (can check via get if exposed)

    def test_revoke_nonexistent_404(self, client):
        """Should return 404 for non-existent key."""
        response = client.delete('/api/user-api-keys/99999')
        assert response.status_code == 404

    def test_revoke_already_revoked_400(self, client):
        """Should return 400 if already revoked."""
        create_response = client.post(
            '/api/user-api-keys',
            json={'name': 'Test', 'scopes': ['read:*']}
        )
        key_id = create_response.json()['id']

        client.delete(f'/api/user-api-keys/{key_id}')
        response = client.delete(f'/api/user-api-keys/{key_id}')
        assert response.status_code == 400
        assert 'already revoked' in response.json()['detail']
