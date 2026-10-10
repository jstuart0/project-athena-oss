"""
Home Assistant Entity Manager
Dynamically fetches and caches HA entities for intelligent device control
"""
import asyncio
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

import httpx
import structlog

from shared.config import get_config
from shared.guest_policy import parse_json_array_env

logger = structlog.get_logger(__name__)

# Room name synonyms. Each phrase is used as an anchor for name matching, so a
# bare token that names another room, a floor, or a furniture/activity word
# does not belong here: it would pull in that room's lights.
ROOM_SYNONYMS = {
    'hall': ['hallway', 'corridor', 'foyer'],
    'hallway': ['hall', 'corridor', 'foyer'],
    'living': ['livingroom', 'living_room', 'lounge'],
    'living room': ['livingroom', 'living_room', 'lounge'],
    'bedroom': ['bed_room'],
    'master': ['master_bedroom', 'main_bedroom'],
    'master bedroom': ['main_bedroom', 'primary_bedroom'],
    'bathroom': ['bath', 'restroom', 'washroom'],
    'bath': ['bathroom', 'restroom', 'washroom'],
    'kitchen': ['kitchenette'],
    'dining': ['diningroom', 'dining_room'],
    'dining room': ['dining', 'diningroom'],
    'office': ['study', 'home_office'],
    'basement': ['cellar'],
    'garage': ['carport'],
    'front': ['front_porch', 'entrance', 'entryway'],
    'back': ['backyard', 'rear', 'patio'],
    'outside': ['outdoor', 'exterior', 'porch', 'patio'],
    'porch': ['front_porch', 'back_porch', 'back_yard'],
}

# Matches a status LED or LED ring by entity id, e.g. a voice satellite's ring
# (light.<device>_led_ring), which is a device indicator and not a room fixture.
DEFAULT_ROOM_LIGHT_EXCLUDE_PATTERNS = [r"(?:^|[._])(?:led_ring|status_led)(?:_|$)"]

# (key, synonym) pairs that only work forward: saying the key reaches the
# synonym, but saying the synonym must not reach the key, because the key is a
# bare word that opens other lights' names (outside_*, back_door_*).
FORWARD_ONLY_SYNONYMS = {('outside', 'porch'), ('outside', 'patio'), ('back', 'patio')}

_ROOM_PART_SPLIT = re.compile(r'\s+and\s+|\s*,\s*|\s*/\s*|\s+or\s+')
_TOKEN_SPLIT = re.compile(r'[\W_]+')
_LEADING_FILLER = ('the',)
_TRAILING_FILLER = ('light', 'lights', 'lamp', 'lamps')


def _tokens(s: str) -> tuple:
    """Unicode-normalised, case-folded word tokens; NFC and NFD forms agree."""
    folded = unicodedata.normalize("NFKC", s or "").casefold()
    return tuple(t for t in _TOKEN_SPLIT.split(folded) if t)


def _split_room(room: str) -> List[tuple]:
    """The requested parts of a room name as token tuples, filler words stripped."""
    parts = []
    for raw in _ROOM_PART_SPLIT.split((room or "").lower().strip()):
        toks = list(_tokens(raw))
        while toks and toks[0] in _LEADING_FILLER:
            toks.pop(0)
        while toks and toks[-1] in _TRAILING_FILLER:
            toks.pop()
        if toks:
            parts.append(tuple(toks))
    return parts


def _room_parts(room: str) -> List[frozenset]:
    """One set of token phrases (the part and its synonyms) per requested part."""
    parts = []
    for part in _split_room(room):
        phrases = {part}
        for key, synonyms in ROOM_SYNONYMS.items():
            key_toks = _tokens(key)
            syn_toks = [_tokens(s) for s in synonyms]
            if part == key_toks:
                phrases.update(syn_toks)
            elif part[:len(key_toks)] != key_toks and any(
                _tokens(syn) == part and (key, syn) not in FORWARD_ONLY_SYNONYMS
                for syn in synonyms
            ):
                # Saying a synonym reaches its key, unless the key is a bare
                # prefix of what was said ("master bedroom" must not reach
                # "master", which also anchors master_bathroom_*).
                phrases.add(key_toks)
        parts.append(frozenset(phrases))
    return parts


def _name_matches(phrase: tuple, name_tokens: tuple, *, anchored: bool) -> bool:
    n = len(phrase)
    if n == 0 or n > len(name_tokens):
        return False
    if anchored:
        return name_tokens[:n] == phrase
    return any(name_tokens[i:i + n] == phrase for i in range(len(name_tokens) - n + 1))


_exclude_cache: tuple = (None, [])


def _clear_room_light_exclude_cache() -> None:
    global _exclude_cache
    _exclude_cache = (None, [])


def _room_light_exclude_patterns() -> list:
    """Compiled exclusion regexes, cached on the raw config string."""
    global _exclude_cache
    raw = get_config().ha_room_light_exclude_entities
    cached_raw, compiled = _exclude_cache
    if cached_raw is not None and cached_raw == (raw or ""):
        return compiled
    compiled = []
    for index, pattern in enumerate(
        parse_json_array_env(raw, DEFAULT_ROOM_LIGHT_EXCLUDE_PATTERNS)
    ):
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            logger.warning("room_light_exclude_pattern_invalid", index=index)
    _exclude_cache = (raw or "", compiled)
    return compiled


def _light_name_token_sets(entity_id: str, entity: Mapping) -> list:
    sets = [_tokens(entity_id.split('.', 1)[-1])]
    friendly = (entity.get('attributes') or {}).get('friendly_name')
    if isinstance(friendly, str) and friendly:
        sets.append(_tokens(friendly))
    return sets


def _room_candidates(parts: Sequence[frozenset], lights: Mapping[str, dict]) -> tuple:
    """Name-tier candidates: (ids, excluded_count, source).

    Each requested part matches names anchored at their first token; a part
    with no anchored match falls back to a whole-token match anywhere in the
    name, for that part only.
    """
    token_sets = {eid: _light_name_token_sets(eid, ent) for eid, ent in lights.items()}
    found = set()
    tiers = set()
    for phrases in parts:
        for anchored, tier in ((True, 'name_anchored'), (False, 'name_any')):
            hits = {
                eid for eid, sets in token_sets.items()
                if any(_name_matches(p, t, anchored=anchored) for p in phrases for t in sets)
            }
            if hits:
                found |= hits
                tiers.add(tier)
                break
    patterns = _room_light_exclude_patterns()
    kept = {eid for eid in found if not any(p.search(eid) for p in patterns)}
    if not kept:
        source = 'none'
    elif len(tiers) == 1:
        source = next(iter(tiers))
    else:
        source = 'mixed'
    return kept, len(found) - len(kept), source


def _member_lists(
    entities: Mapping, extra_members: Optional[Mapping[str, Sequence[str]]]
):
    def members_of(entity_id: str) -> Optional[Sequence[str]]:
        attr = ((entities.get(entity_id) or {}).get('attributes') or {}).get('entity_id')
        if isinstance(attr, list) and attr:
            return attr
        extra = (extra_members or {}).get(entity_id)
        if extra:
            return list(extra)
        return None
    return members_of


def _reach(entity_id: str, members_of) -> set:
    """Every id reachable through one or more member steps (may include itself in a cycle)."""
    seen = set()
    stack = list(members_of(entity_id) or ())
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(members_of(cur) or ())
    return seen


def expand_light_leaves(
    entity_ids: Iterable[str],
    entities: Mapping,
    extra_members: Optional[Mapping[str, Sequence[str]]] = None,
) -> Dict[str, frozenset]:
    """Map each id to the physical (non-group) lights beneath it.

    Group membership comes from ``entities[id]["attributes"]["entity_id"]``;
    ``extra_members[id]`` is consulted only when the entity map has no list for
    that id. An id with no known members, including one absent from the map,
    is its own leaf. Cycle-safe: each group is visited once, and a group whose
    traversal reaches no leaf (a pure cycle) is its own leaf.
    """
    members_of = _member_lists(entities, extra_members)
    result = {}
    for entity_id in entity_ids:
        leaves = set()
        visited = set()
        stack = [entity_id]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            members = members_of(cur)
            if members is None:
                leaves.add(cur)
            else:
                stack.extend(members)
        result[entity_id] = frozenset(leaves or {entity_id})
    return result


def cover_light_targets(
    ids: Iterable[str],
    entities: Mapping,
    room_tokens=(),
    extra_members: Optional[Mapping[str, Sequence[str]]] = None,
) -> List[str]:
    """Smallest deterministic set of ids whose leaves cover the input leaves exactly once.

    ``room_tokens`` is one token tuple, or a sequence of them (one per requested
    part); a candidate named exactly like a part wins ties between equal covers.
    The multiset of leaves under the returned ids has no duplicates.
    """
    if room_tokens and isinstance(room_tokens[0], str):
        room_tokens = (tuple(room_tokens),)
    room_phrases = {tuple(p) for p in room_tokens}
    members_of = _member_lists(entities, extra_members)
    candidates = sorted(set(ids))
    leaves = expand_light_leaves(candidates, entities, extra_members)
    reach = {c: _reach(c, members_of) for c in candidates}

    def dominated(c: str) -> bool:
        return any(
            d != c and c in reach[d] and (d not in reach[c] or d < c)
            for d in candidates
        )

    def exact_room_name(c: str) -> bool:
        return any(
            t in room_phrases
            for t in _light_name_token_sets(c, entities.get(c) or {})
        )

    remaining = [c for c in candidates if not dominated(c)]
    remaining.sort(key=lambda c: (-len(leaves[c]), not exact_room_name(c), len(c), c))

    covered = set()
    chosen = set()
    for c in remaining:
        own = leaves[c]
        if own <= covered:
            continue
        if own.isdisjoint(covered):
            chosen.add(c)
        else:
            chosen.update(own - covered)
        covered |= own
    return sorted(chosen)


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
    
    async def find_lights_by_room(self, room_name: str) -> List[Dict]:
        """Find the light entities that cover a room (compound names like 'hall and hallway' work).

        Contract: each returned id is meant to be written once, and no physical
        light sits under two returned ids. An empty list means the room has no
        lights. Order is deterministic (sorted by entity id).
        """
        await self.get_entities()

        parts = _room_parts(room_name)
        lights = self._entities_by_type.get('light', {})
        candidates, excluded, source = _room_candidates(parts, lights)
        targets = cover_light_targets(
            candidates, self._entities_cache, _split_room(room_name)
        )

        logger.info(
            "room_lights_resolved",
            source=source,
            candidates=len(candidates),
            excluded=excluded,
            targets=len(targets),
        )
        return [self._light_match(entity_id) for entity_id in targets]

    def _light_match(self, entity_id: str) -> Dict:
        info = self._light_groups.get(entity_id)
        if info is None:
            entity = (self._entities_cache or {}).get(entity_id) or {}
            attrs = entity.get('attributes') or {}
            return {
                'entity_id': entity_id,
                'friendly_name': attrs.get('friendly_name', entity_id),
                'members': [],
                'state': entity.get('state', 'unknown'),
                'type': 'individual',
            }
        is_group = info.get('is_group', len(info.get('members', [])) > 0)
        return {
            'entity_id': entity_id,
            'friendly_name': info['friendly_name'],
            'members': info['members'],
            'state': info['state'],
            'type': 'group' if is_group else 'individual',
        }

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
