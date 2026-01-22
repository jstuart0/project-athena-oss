"""
Home Assistant Entity Manager
Dynamically fetches and caches HA entities for intelligent device control
"""
import asyncio
import httpx
from typing import Dict, List, Optional
from datetime import datetime, timedelta

class HAEntityManager:
    def __init__(self, ha_url: str, ha_token: str):
        self.ha_url = ha_url
        self.ha_token = ha_token
        self.headers = {
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json"
        }
        self.client = httpx.AsyncClient(
            base_url=ha_url,
            headers=self.headers,
            verify=False,
            timeout=30.0
        )
        
        # Cache
        self._entities_cache = None
        self._cache_time = None
        self._cache_duration = timedelta(minutes=5)
        
        # Indexed lookups
        self._entities_by_area = {}
        self._entities_by_type = {}
        self._light_groups = {}
    
    async def refresh_entities(self):
        """Fetch all entities from Home Assistant"""
        response = await self.client.get("/api/states")
        response.raise_for_status()
        
        entities = response.json()
        self._entities_cache = {e['entity_id']: e for e in entities}
        self._cache_time = datetime.now()
        
        # Build indexes
        self._build_indexes()
        
        return self._entities_cache
    
    def _build_indexes(self):
        """Build lookup indexes for fast querying"""
        self._entities_by_area = {}
        self._entities_by_type = {}
        self._light_groups = {}
        
        for entity_id, entity in self._entities_cache.items():
            # Index by domain (light, switch, etc)
            domain = entity_id.split('.')[0]
            if domain not in self._entities_by_type:
                self._entities_by_type[domain] = {}
            self._entities_by_type[domain][entity_id] = entity
            
            # Index light groups AND individual lights
            if domain == 'light':
                attrs = entity.get('attributes', {})
                if 'entity_id' in attrs and isinstance(attrs['entity_id'], list):
                    # This is a group - store its members
                    self._light_groups[entity_id] = {
                        'friendly_name': attrs.get('friendly_name', entity_id),
                        'members': attrs['entity_id'],
                        'state': entity.get('state'),
                        'is_group': True
                    }
                else:
                    # Individual light - store without members
                    self._light_groups[entity_id] = {
                        'friendly_name': attrs.get('friendly_name', entity_id),
                        'members': [],  # No members - it's an individual light
                        'state': entity.get('state'),
                        'is_group': False
                    }
    
    async def get_entities(self, force_refresh=False) -> Dict:
        """Get cached entities or refresh if needed"""
        if force_refresh or self._entities_cache is None or \
           (datetime.now() - self._cache_time) > self._cache_duration:
            await self.refresh_entities()
        
        return self._entities_cache
    
    # Room name synonyms for flexible matching
    ROOM_SYNONYMS = {
        'hall': ['hallway', 'corridor', 'entrance', 'foyer'],
        'hallway': ['hall', 'corridor', 'entrance', 'foyer'],
        'living': ['livingroom', 'living_room', 'lounge', 'family'],
        'living room': ['livingroom', 'living_room', 'lounge', 'family'],
        'bedroom': ['bed_room', 'bed'],
        'master': ['master_bedroom', 'main_bedroom', 'primary'],
        'master bedroom': ['master', 'main_bedroom', 'primary'],
        'bathroom': ['bath', 'restroom', 'washroom'],
        'bath': ['bathroom', 'restroom', 'washroom'],
        'kitchen': ['kitchenette'],
        'dining': ['diningroom', 'dining_room'],
        'dining room': ['dining', 'diningroom'],
        'office': ['study', 'home_office', 'work'],
        'basement': ['cellar', 'downstairs'],
        'garage': ['carport'],
        'front': ['front_porch', 'entrance', 'entryway'],
        'back': ['backyard', 'rear', 'patio'],
        'outside': ['outdoor', 'exterior', 'porch', 'patio'],
        # Round 17: Added porch synonyms
        'porch': ['front_porch', 'back_porch', 'outdoor', 'outside', 'exterior', 'front', 'back_yard'],
    }

    def _expand_room_names(self, room_name: str) -> List[str]:
        """Expand a room name into all possible search terms including synonyms"""
        room_lower = room_name.lower().strip()

        # Split on common conjunctions and punctuation
        # Handle "hall and hallway", "hall, hallway", "hall/hallway", etc.
        import re
        parts = re.split(r'\s+and\s+|\s*,\s*|\s*/\s*|\s+or\s+', room_lower)

        # Collect all search terms
        search_terms = set()
        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Add the part itself (both with spaces and underscores)
            search_terms.add(part.replace(' ', '_'))
            search_terms.add(part.replace('_', ' '))

            # Add synonyms
            for key, synonyms in self.ROOM_SYNONYMS.items():
                if part == key or part.replace(' ', '_') == key.replace(' ', '_'):
                    for syn in synonyms:
                        search_terms.add(syn.replace(' ', '_'))
                        search_terms.add(syn.replace('_', ' '))
                # Also check if any synonym matches
                for syn in synonyms:
                    if part == syn or part.replace(' ', '_') == syn.replace(' ', '_'):
                        search_terms.add(key.replace(' ', '_'))
                        search_terms.add(key.replace('_', ' '))

        return list(search_terms)

    async def find_lights_by_room(self, room_name: str) -> List[Dict]:
        """Find light entities for a specific room (supports compound names like 'hall and hallway')"""
        await self.get_entities()

        # Expand room name into all possible search terms
        search_terms = self._expand_room_names(room_name)

        # Look for light groups matching any search term
        matches = []
        seen_entity_ids = set()  # Avoid duplicates

        for entity_id, group_info in self._light_groups.items():
            if entity_id in seen_entity_ids:
                continue

            friendly_name = group_info['friendly_name'].lower()
            # Normalize entity name the same way
            entity_name_normalized = entity_id.replace('light.', '').lower()
            friendly_normalized = friendly_name.replace(' ', '_')

            # Check if any search term matches
            for search_term in search_terms:
                if search_term in entity_name_normalized or search_term in friendly_normalized:
                    is_group = group_info.get('is_group', len(group_info.get('members', [])) > 0)
                    matches.append({
                        'entity_id': entity_id,
                        'friendly_name': group_info['friendly_name'],
                        'members': group_info['members'],
                        'state': group_info['state'],
                        'type': 'group' if is_group else 'individual'
                    })
                    seen_entity_ids.add(entity_id)
                    break  # Found a match, no need to check other terms

        return matches

    async def get_all_light_groups(self) -> List[Dict]:
        """Get all light groups in the house for whole-house commands"""
        await self.get_entities()

        all_groups = []
        for entity_id, group_info in self._light_groups.items():
            is_group = group_info.get('is_group', len(group_info.get('members', [])) > 0)
            # Only include groups (not individual lights) for whole-house commands
            if is_group and group_info.get('members'):
                all_groups.append({
                    'entity_id': entity_id,
                    'friendly_name': group_info['friendly_name'],
                    'members': group_info['members'],
                    'state': group_info['state'],
                    'type': 'group'
                })

        return all_groups

    async def get_light_capabilities(self, entity_id: str) -> Dict:
        """Get capabilities of a light (color, brightness, etc)"""
        await self.get_entities()

        entity = self._entities_cache.get(entity_id)
        if not entity:
            return {}

        attrs = entity.get('attributes', {})
        return {
            'supports_color': 'hs' in attrs.get('supported_color_modes', []) or \
                            'rgb' in attrs.get('supported_color_modes', []),
            'supports_brightness': 'brightness' in attrs.get('supported_color_modes', []) or \
                                 attrs.get('brightness') is not None,
            'supports_color_temp': 'color_temp' in attrs.get('supported_color_modes', []),
            'current_state': entity.get('state'),
            'friendly_name': attrs.get('friendly_name', entity_id)
        }

    async def get_climate_state(self, entity_id: str = "climate.thermostat") -> Optional[Dict]:
        """Get current state of climate/thermostat entity"""
        await self.get_entities()

        entity = self._entities_cache.get(entity_id)
        if not entity:
            # Try to find any climate entity
            climate_entities = self._entities_by_type.get('climate', {})
            if climate_entities:
                entity_id = list(climate_entities.keys())[0]
                entity = climate_entities[entity_id]
            else:
                return None

        attrs = entity.get('attributes', {})
        # Handle both single-setpoint (temperature) and dual-setpoint (target_temp_high/low) modes
        target_temp = attrs.get('temperature')
        if target_temp is None:
            # Dual-setpoint mode (heat_cool) - use high/low temps
            target_temp_high = attrs.get('target_temp_high')
            target_temp_low = attrs.get('target_temp_low')
            if target_temp_high is not None and target_temp_low is not None:
                # Return the midpoint as target_temp, but also include high/low
                target_temp = (target_temp_high + target_temp_low) / 2
        return {
            'entity_id': entity_id,
            'state': entity.get('state'),  # heat, cool, off, heat_cool
            'current_temp': attrs.get('current_temperature'),
            'target_temp': target_temp,
            'target_temp_high': attrs.get('target_temp_high'),
            'target_temp_low': attrs.get('target_temp_low'),
            'hvac_action': attrs.get('hvac_action'),  # heating, cooling, idle
            'humidity': attrs.get('current_humidity'),
            'hvac_modes': attrs.get('hvac_modes', []),
            'min_temp': attrs.get('min_temp'),
            'max_temp': attrs.get('max_temp'),
            'friendly_name': attrs.get('friendly_name', 'Thermostat')
        }

    async def get_all_climate_entities(self) -> List[Dict]:
        """Get all climate/thermostat entities"""
        await self.get_entities()

        climate_entities = self._entities_by_type.get('climate', {})
        results = []

        for entity_id, entity in climate_entities.items():
            attrs = entity.get('attributes', {})
            results.append({
                'entity_id': entity_id,
                'state': entity.get('state'),
                'current_temp': attrs.get('current_temperature'),
                'target_temp': attrs.get('temperature'),
                'friendly_name': attrs.get('friendly_name', entity_id)
            })

        return results
