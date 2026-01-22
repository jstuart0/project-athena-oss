"""Polyline utilities for route processing.

Provides functions for decoding Google Maps polylines and
calculating points along routes.
"""

import math
from typing import List, Tuple


def decode_polyline(encoded: str) -> List[Tuple[float, float]]:
    """Decode a Google Maps encoded polyline string.

    The encoding algorithm is documented at:
    https://developers.google.com/maps/documentation/utilities/polylinealgorithm

    Args:
        encoded: Encoded polyline string

    Returns:
        List of (latitude, longitude) tuples
    """
    if not encoded:
        return []

    points = []
    index = 0
    lat = 0
    lng = 0

    while index < len(encoded):
        # Decode latitude
        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1f) << shift
            shift += 5
            if b < 0x20:
                break

        dlat = ~(result >> 1) if result & 1 else result >> 1
        lat += dlat

        # Decode longitude
        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1f) << shift
            shift += 5
            if b < 0x20:
                break

        dlng = ~(result >> 1) if result & 1 else result >> 1
        lng += dlng

        # Convert to decimal degrees (Google uses 1e-5 precision)
        points.append((lat / 1e5, lng / 1e5))

    return points


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great-circle distance between two points in meters.

    Uses the Haversine formula.

    Args:
        lat1, lon1: First point coordinates in decimal degrees
        lat2, lon2: Second point coordinates in decimal degrees

    Returns:
        Distance in meters
    """
    # Earth's radius in meters
    R = 6371000

    # Convert to radians
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    # Haversine formula
    a = math.sin(delta_phi / 2) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


def get_point_at_fraction(points: List[Tuple[float, float]], fraction: float) -> Tuple[float, float]:
    """Get the point at a given fraction along a polyline.

    Args:
        points: List of (lat, lng) points defining the polyline
        fraction: Position along the route (0.0 = start, 1.0 = end)

    Returns:
        (latitude, longitude) tuple at the specified position

    Raises:
        ValueError: If points list is empty
    """
    if not points:
        raise ValueError("Points list cannot be empty")

    if len(points) == 1:
        return points[0]

    # Clamp fraction to valid range
    fraction = max(0.0, min(1.0, fraction))

    # Handle edge cases
    if fraction == 0:
        return points[0]
    if fraction == 1:
        return points[-1]

    # Calculate total distance
    total_distance = 0.0
    segment_distances = []

    for i in range(len(points) - 1):
        dist = haversine_distance(
            points[i][0], points[i][1],
            points[i + 1][0], points[i + 1][1]
        )
        segment_distances.append(dist)
        total_distance += dist

    if total_distance == 0:
        return points[0]

    # Find the target distance
    target_distance = total_distance * fraction

    # Walk through segments to find the target point
    accumulated_distance = 0.0

    for i, seg_dist in enumerate(segment_distances):
        if accumulated_distance + seg_dist >= target_distance:
            # Target is within this segment
            if seg_dist == 0:
                return points[i]

            # Calculate position within segment
            remaining = target_distance - accumulated_distance
            segment_fraction = remaining / seg_dist

            # Linear interpolation
            lat = points[i][0] + segment_fraction * (points[i + 1][0] - points[i][0])
            lng = points[i][1] + segment_fraction * (points[i + 1][1] - points[i][1])

            return (lat, lng)

        accumulated_distance += seg_dist

    # Fallback to last point
    return points[-1]


def get_bounding_box(
    points: List[Tuple[float, float]],
    padding_meters: float = 0
) -> Tuple[float, float, float, float]:
    """Get the bounding box for a list of points.

    Args:
        points: List of (lat, lng) points
        padding_meters: Optional padding to add to the box

    Returns:
        Tuple of (min_lat, min_lng, max_lat, max_lng)
    """
    if not points:
        raise ValueError("Points list cannot be empty")

    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]

    min_lat = min(lats)
    max_lat = max(lats)
    min_lng = min(lngs)
    max_lng = max(lngs)

    if padding_meters > 0:
        # Approximate padding in degrees (rough, works at mid-latitudes)
        lat_padding = padding_meters / 111000  # ~111km per degree latitude
        lng_padding = padding_meters / (111000 * math.cos(math.radians((min_lat + max_lat) / 2)))

        min_lat -= lat_padding
        max_lat += lat_padding
        min_lng -= lng_padding
        max_lng += lng_padding

    return (min_lat, min_lng, max_lat, max_lng)
