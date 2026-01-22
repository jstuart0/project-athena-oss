"""Category mappings for place searches along routes.

Maps common categories to Google Places API types and handles
position-to-fraction conversions for route placement.
"""

from typing import List

# Category to Google Places API types mapping
CATEGORY_MAPPINGS = {
    # Food & Dining
    "food": ["restaurant", "meal_takeaway"],
    "restaurant": ["restaurant"],
    "fast food": ["fast_food_restaurant"],
    "cafe": ["cafe", "coffee_shop"],
    "coffee": ["cafe", "coffee_shop"],
    "pizza": ["pizza_restaurant"],
    "mexican": ["mexican_restaurant"],
    "chinese": ["chinese_restaurant"],
    "italian": ["italian_restaurant"],
    "breakfast": ["breakfast_restaurant", "cafe"],
    "lunch": ["restaurant", "fast_food_restaurant"],
    "dinner": ["restaurant"],

    # Brand-specific (common chains)
    "starbucks": ["cafe"],
    "mcdonalds": ["fast_food_restaurant"],
    "dunkin": ["cafe"],
    "wendys": ["fast_food_restaurant"],
    "burger king": ["fast_food_restaurant"],
    "chick-fil-a": ["fast_food_restaurant"],
    "taco bell": ["fast_food_restaurant"],
    "subway": ["fast_food_restaurant"],
    "panera": ["bakery", "cafe"],

    # Fuel & Charging
    "gas": ["gas_station"],
    "gas station": ["gas_station"],
    "fuel": ["gas_station"],
    "ev charging": ["electric_vehicle_charging_station"],
    "charging station": ["electric_vehicle_charging_station"],
    "ev": ["electric_vehicle_charging_station"],

    # Rest & Facilities
    "rest stop": ["rest_stop"],
    "rest area": ["rest_stop"],
    "bathroom": ["rest_stop", "gas_station"],
    "restroom": ["rest_stop", "gas_station"],

    # Shopping
    "grocery": ["supermarket", "grocery_store"],
    "supermarket": ["supermarket"],
    "pharmacy": ["pharmacy"],
    "drugstore": ["pharmacy", "drugstore"],
    "convenience": ["convenience_store"],
    "convenience store": ["convenience_store"],
    "atm": ["atm"],
    "bank": ["bank"],

    # Lodging
    "hotel": ["hotel", "lodging"],
    "motel": ["motel", "lodging"],
    "lodging": ["lodging"],

    # Entertainment
    "park": ["park"],
    "attraction": ["tourist_attraction"],
    "museum": ["museum"],

    # Services
    "hospital": ["hospital"],
    "urgent care": ["urgent_care_center"],
    "mechanic": ["car_repair"],
    "car repair": ["car_repair"],
    "tire shop": ["tire_shop"],
}

# Position to fraction mapping
POSITION_FRACTIONS = {
    "beginning": 0.15,
    "near the beginning": 0.15,
    "start": 0.15,
    "early": 0.25,
    "quarter": 0.25,
    "first quarter": 0.25,
    "halfway": 0.5,
    "middle": 0.5,
    "midpoint": 0.5,
    "half": 0.5,
    "three quarters": 0.75,
    "three_quarters": 0.75,
    "later": 0.75,
    "near the end": 0.85,
    "end": 0.85,
    "before destination": 0.85,
}


def get_place_types(category: str) -> List[str]:
    """Get Google Places API types for a category.

    Args:
        category: Category name (case-insensitive)

    Returns:
        List of Google Places API type strings
    """
    normalized = category.lower().strip()

    # Direct match
    if normalized in CATEGORY_MAPPINGS:
        return CATEGORY_MAPPINGS[normalized]

    # Check if any key contains the search term
    for key, types in CATEGORY_MAPPINGS.items():
        if normalized in key or key in normalized:
            return types

    # Default to restaurant for food-related unknown categories
    return ["restaurant"]


def get_position_fraction(position: str) -> float:
    """Get the route fraction for a position description.

    Args:
        position: Position description (e.g., "halfway", "beginning", "end")

    Returns:
        Fraction of route (0.0 to 1.0)
    """
    normalized = position.lower().strip()

    # Direct match
    if normalized in POSITION_FRACTIONS:
        return POSITION_FRACTIONS[normalized]

    # Check for partial matches
    for key, fraction in POSITION_FRACTIONS.items():
        if normalized in key or key in normalized:
            return fraction

    # Default to halfway
    return 0.5
