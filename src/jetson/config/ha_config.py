# Home Assistant Configuration for Athena Lite
# Configure via environment variables for your installation
import os

HOME_ASSISTANT_URL = os.getenv('HA_URL', '')
HOME_ASSISTANT_TOKEN = os.getenv('HA_TOKEN', '')

# Test connectivity
import requests
def test_ha_connection():
    headers = {'Authorization': f'Bearer {HOME_ASSISTANT_TOKEN}'}
    try:
        response = requests.get(f'{HOME_ASSISTANT_URL}/api/', headers=headers, verify=False)
        return response.status_code == 200
    except Exception as e:
        return False
