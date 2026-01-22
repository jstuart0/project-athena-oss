"""
Query Complexity Detection

Determines query complexity using feature extraction and intent baseline.
No LLM call required - fast and deterministic.

Complexity Levels:
- simple: Single fact lookup, basic command
- complex: Comparisons, temporal reasoning, preferences, relative locations
- super_complex: Multi-tool coordination, synthesis, conditional planning
"""

import re
import structlog
from dataclasses import dataclass, asdict
from typing import Optional

logger = structlog.get_logger("orchestrator")


@dataclass
class ComplexityFeatures:
    """Features extracted from query for complexity scoring."""
    word_count: int
    has_comparison: bool
    has_temporal: bool
    has_conditional: bool
    has_aggregation: bool
    has_relative_location: bool
    has_multi_location: bool
    has_multi_entity: bool
    conjunction_count: int
    question_complexity: int  # 0=simple, 1=decision, 2=explanatory

    def score(self) -> int:
        """Compute raw complexity score from features."""
        score = 0

        # Word count (longer queries tend to be more complex)
        if self.word_count > 25:
            score += 3
        elif self.word_count > 15:
            score += 2
        elif self.word_count > 8:
            score += 1

        # High-signal complexity indicators
        if self.has_comparison:
            score += 4  # "compare X and Y", "X vs Y"
        if self.has_conditional:
            score += 3  # "if", "when", "should I"
        if self.has_aggregation:
            score += 3  # "all", "summary", "list all"
        if self.has_multi_location:
            score += 3  # Multiple places mentioned
        if self.has_multi_entity:
            score += 2  # Multiple devices/items

        # Medium-signal indicators
        if self.has_temporal:
            score += 2  # "tomorrow", "next week"
        if self.has_relative_location:
            score += 1  # "near me", "close to"

        # Conjunction complexity
        if self.conjunction_count > 2:
            score += 3
        elif self.conjunction_count > 1:
            score += 2
        elif self.conjunction_count == 1:
            score += 1

        # Question type complexity
        score += self.question_complexity

        return score


# Intent baseline complexities (empirically tuned)
INTENT_COMPLEXITY_BASELINE = {
    # Simple by default - single fact lookups
    "weather": 0,
    "sports": 0,
    "control": 0,
    "music_play": 0,
    "music_control": 0,
    "stocks": 0,
    "airports": 0,

    # Moderately complex - often involve search/filtering
    "dining": 2,
    "events": 2,
    "directions": 2,
    "flights": 2,
    "streaming": 2,
    "recipes": 2,
    "news": 1,

    # Complex by default - catch-all or multi-faceted
    "general_info": 3,
    "text_me_that": 1,
    "unknown": 2,
}

# Complexity thresholds (tunable)
COMPLEX_THRESHOLD = 4
SUPER_COMPLEX_THRESHOLD = 8


def extract_complexity_features(query: str) -> ComplexityFeatures:
    """Extract complexity-relevant features from a query."""
    q = query.lower()
    words = query.split()

    # Comparison patterns (high signal)
    has_comparison = bool(re.search(
        r'\b(compare|comparing|comparison|vs\.?|versus|better|worse|'
        r'between|difference|differ|rather than|instead of|prefer)\b', q
    )) or bool(re.search(r'\bor\b.*\bor\b', q))  # Multiple "or"s suggest comparison

    # Temporal patterns
    has_temporal = bool(re.search(
        r'\b(tomorrow|yesterday|today|tonight|next|last|this|later|soon|'
        r'monday|tuesday|wednesday|thursday|friday|saturday|sunday|'
        r'week|month|year|morning|afternoon|evening|night|'
        r'hour|minute|upcoming|scheduled|planning|plan)\b', q
    ))

    # Conditional patterns
    has_conditional = bool(re.search(
        r'\b(if|when|unless|whether|should i|would i|could i|might|'
        r'depending|based on|in case|assuming|suppose|what if)\b', q
    ))

    # Aggregation/synthesis patterns
    has_aggregation = bool(re.search(
        r'\b(all|every|each|summary|summarize|list|overview|recap|'
        r'multiple|several|various|different|show me all|everything|'
        r'both|combine|together|and also)\b', q
    ))

    # Relative location patterns
    has_relative_location = bool(re.search(
        r'\b(near|nearby|close to|around|by me|near me|nearest|closest|'
        r'within|walking distance|drive from|from here|from home|'
        r'in my area|local|neighborhood)\b', q
    ))

    # Multiple location detection (e.g., "weather in NYC and LA")
    location_matches = re.findall(
        r'\b(?:in|at|to|from|near)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)', query
    )
    has_multi_location = len(set(location_matches)) > 1

    # Multiple entity detection (e.g., "lights and fan", "kitchen and bedroom")
    entity_patterns = re.findall(
        r'\b(light|lights|fan|switch|thermostat|blind|blinds|door|lock|'
        r'tv|television|speaker|camera|sensor|plug|outlet|'
        r'kitchen|bedroom|bathroom|living room|office|garage|basement)\b', q
    )
    has_multi_entity = len(set(entity_patterns)) > 1

    # Count conjunctions
    conjunction_count = len(re.findall(r'\b(and|or|but|also|plus|then|after)\b', q))

    # Question complexity
    question_complexity = 0
    if re.search(r'\b(why|explain|how does|how do|what causes|tell me about|'
                 r'what is the reason|understand)\b', q):
        question_complexity = 2  # Explanatory questions need reasoning
    elif re.search(r'\b(which|what.*best|what.*should|recommend|suggest|'
                   r'what do you think|should i|better)\b', q):
        question_complexity = 1  # Decision questions need evaluation

    return ComplexityFeatures(
        word_count=len(words),
        has_comparison=has_comparison,
        has_temporal=has_temporal,
        has_conditional=has_conditional,
        has_aggregation=has_aggregation,
        has_relative_location=has_relative_location,
        has_multi_location=has_multi_location,
        has_multi_entity=has_multi_entity,
        conjunction_count=conjunction_count,
        question_complexity=question_complexity,
    )


def determine_complexity(
    query: str,
    intent: Optional[str] = None,
    log_features: bool = True
) -> str:
    """
    Determine query complexity using feature extraction and intent baseline.

    Args:
        query: The user's query text
        intent: Optional intent category (improves accuracy)
        log_features: Whether to log feature breakdown for debugging

    Returns:
        "simple", "complex", or "super_complex"
    """
    # Extract features
    features = extract_complexity_features(query)
    feature_score = features.score()

    # Add intent baseline
    intent_baseline = INTENT_COMPLEXITY_BASELINE.get(intent, 1) if intent else 1
    total_score = feature_score + intent_baseline

    # Determine complexity level
    if total_score >= SUPER_COMPLEX_THRESHOLD:
        complexity = "super_complex"
    elif total_score >= COMPLEX_THRESHOLD:
        complexity = "complex"
    else:
        complexity = "simple"

    # Log for debugging/tuning
    if log_features:
        # Build list of triggered features for easier debugging
        triggered = []
        if features.has_comparison:
            triggered.append("comparison(+4)")
        if features.has_conditional:
            triggered.append("conditional(+3)")
        if features.has_aggregation:
            triggered.append("aggregation(+3)")
        if features.has_multi_location:
            triggered.append("multi_location(+3)")
        if features.has_multi_entity:
            triggered.append("multi_entity(+2)")
        if features.has_temporal:
            triggered.append("temporal(+2)")
        if features.has_relative_location:
            triggered.append("relative_loc(+1)")
        if features.conjunction_count > 0:
            triggered.append(f"conjunctions({features.conjunction_count})")
        if features.question_complexity > 0:
            triggered.append(f"q_type(+{features.question_complexity})")
        if features.word_count > 15:
            triggered.append(f"words({features.word_count})")

        triggered_str = ", ".join(triggered) if triggered else "none"
        logger.info(
            "complexity_analysis",
            complexity=complexity.upper(),
            total_score=total_score,
            feature_score=feature_score,
            intent_baseline=intent_baseline,
            triggers=triggered_str,
            query=query[:50]
        )
        logger.debug("complexity_features", features=asdict(features))

    return complexity


def get_complexity_with_override(
    query: str,
    intent: Optional[str] = None,
    cached_complexity: Optional[str] = None,
    is_followup: bool = False
) -> str:
    """
    Get complexity with support for overrides and special cases.

    Args:
        query: The user's query text
        intent: Intent category
        cached_complexity: Complexity from cache (if available)
        is_followup: Whether this is a follow-up question

    Returns:
        "simple", "complex", or "super_complex"
    """
    # Follow-up questions inherit parent complexity or default to simple
    if is_followup:
        # Simple follow-ups stay simple, but check for complexity indicators
        features = extract_complexity_features(query)
        if features.has_comparison or features.has_conditional:
            return "complex"
        return cached_complexity or "simple"

    # Use cached value if available and query hasn't changed significantly
    if cached_complexity:
        # But still check for upgrade indicators
        features = extract_complexity_features(query)
        if features.score() >= SUPER_COMPLEX_THRESHOLD:
            return "super_complex"
        if features.score() >= COMPLEX_THRESHOLD and cached_complexity == "simple":
            return "complex"
        return cached_complexity

    # Full analysis
    return determine_complexity(query, intent)


# Quick test examples
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    test_queries = [
        ("What's the weather?", "weather"),
        ("Turn on the kitchen lights", "control"),
        ("Compare the weather in Baltimore and New York", "weather"),
        ("Will it rain tomorrow afternoon?", "weather"),
        ("Find me Italian restaurants near me that are open late", "dining"),
        ("What's the best route to the airport if there's traffic?", "directions"),
        ("Turn off all the lights except the bedroom", "control"),
        ("Plan my day - check weather, find cafes, remind me about meetings", "general_info"),
        ("Which restaurant is better, the Italian place or the Thai one?", "dining"),
        ("Play some jazz music", "music_play"),
        ("What are the sports scores?", "sports"),
        ("Show me all the news about technology and also sports updates", "news"),
    ]

    print("\n" + "="*80)
    print("COMPLEXITY DETECTION TEST")
    print("="*80)

    for query, intent in test_queries:
        complexity = determine_complexity(query, intent, log_features=False)
        features = extract_complexity_features(query)
        print(f"\n[{complexity.upper():^13}] {query}")
        print(f"              Intent: {intent}, Score: {features.score() + INTENT_COMPLEXITY_BASELINE.get(intent, 1)}")
