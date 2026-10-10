"""
TTS Text Normalizer for Athena Voice Output

Expands abbreviations and formats text for natural speech synthesis.
Handles street names, state abbreviations, and common acronyms.
"""

import re
from typing import Dict

# A number that starts a unit rule. The start guard keeps every digit run to
# one match attempt (linear time on "1" * N), and the bounded quantifiers cap
# each attempt. A run of more than nine digits is therefore left alone.
_NUM_START = r'(?<![\d.])'
_NUM = _NUM_START + r'(-?\d{1,9}(?:\.\d{1,9})?)'
_GAP = r'\s{0,3}'


def _unit(count: str, singular: str, plural: str) -> str:
    """Pick the unit word for a matched number: only exactly 1 / -1 is singular."""
    return singular if count in ('1', '-1') else plural


# Street/road abbreviations - must come AFTER a number or street name
STREET_ABBREVIATIONS: Dict[str, str] = {
    r'\bSt\b': 'Street',
    r'\bAve\b': 'Avenue',
    r'\bBlvd\b': 'Boulevard',
    r'\bDr\b': 'Drive',  # Context-sensitive - handled separately
    r'\bRd\b': 'Road',
    r'\bLn\b': 'Lane',
    r'\bCt\b': 'Court',
    r'\bPl\b': 'Place',
    r'\bCir\b': 'Circle',
    r'\bPkwy\b': 'Parkway',
    r'\bHwy\b': 'Highway',
    r'\bTer\b': 'Terrace',
    r'\bWay\b': 'Way',
    r'\bSq\b': 'Square',
    r'\bTpke\b': 'Turnpike',
    r'\bFwy\b': 'Freeway',
    r'\bExpy\b': 'Expressway',
}

# US State abbreviations
STATE_ABBREVIATIONS: Dict[str, str] = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'D.C.',
}

# Common abbreviations that should be expanded
COMMON_ABBREVIATIONS: Dict[str, str] = {
    r'\bMr\.\b': 'Mister',
    r'\bMrs\.\b': 'Missus',
    r'\bMs\.\b': 'Miss',
    r'\bDr\.': 'Doctor',  # When followed by a period (title)
    r'\bProf\.\b': 'Professor',
    r'\bSr\.\b': 'Senior',
    r'\bJr\.\b': 'Junior',
    r'\bNo\.\b': 'Number',
    r'\bvs\.?\b': 'versus',
    r'\betc\.\b': 'etcetera',
    r'\be\.g\.\b': 'for example',
    r'\bi\.e\.\b': 'that is',
    r'\bapprox\.\b': 'approximately',
    r'\bmin\b': 'minutes',
    r'\bmins\b': 'minutes',
    r'\bhr\b': 'hour',
    r'\bhrs\b': 'hours',
    r'\bft\b': 'feet',
    r'\bmi\b': 'miles',
    r'\bsq ft\b': 'square feet',
    r'\bmph\b': 'miles per hour',
}

# Directional abbreviations (for addresses)
DIRECTION_ABBREVIATIONS: Dict[str, str] = {
    r'\bN\.?\b': 'North',
    r'\bS\.?\b': 'South',
    r'\bE\.?\b': 'East',
    r'\bW\.?\b': 'West',
    r'\bNE\b': 'Northeast',
    r'\bNW\b': 'Northwest',
    r'\bSE\b': 'Southeast',
    r'\bSW\b': 'Southwest',
}


def expand_street_abbreviations(text: str) -> str:
    """Expand street name abbreviations in addresses."""
    # Pattern: number followed by street name and abbreviation
    # e.g., "123 Main St" -> "123 Main Street"

    for abbrev, full in STREET_ABBREVIATIONS.items():
        # Only expand if it looks like an address context
        # (preceded by a number or common street name patterns)
        text = re.sub(abbrev, full, text, flags=re.IGNORECASE)

    return text


_SINGLE_WORD_STATES = frozenset(name for name in STATE_ABBREVIATIONS.values() if name.isalpha())


def expand_state_abbreviations(text: str) -> str:
    """Expand US state abbreviations."""
    # Pattern: comma + space + two-letter state code (optionally followed by ZIP)
    # e.g., "Denver, CO" -> "Denver, Colorado"
    # e.g., "Denver, CO 80202" -> "Denver, Colorado 80202"
    # Also: "Denver CO" -> "Denver Colorado" (no comma)

    for abbrev, full in STATE_ABBREVIATIONS.items():
        if abbrev not in text:
            continue

        # Match state abbreviation after comma or at word boundary
        # Avoid matching in the middle of words
        pattern = rf',\s*{abbrev}\b'
        replacement = f', {full}'
        text = re.sub(pattern, replacement, text)

        # Match standalone state abbreviations (less aggressive)
        pattern = rf'\b{abbrev}\b(?=\s*\d{{5}}|\s*$|\s*[,.])'
        text = re.sub(pattern, full, text)

        # Match state abbreviation after city name (word + space + STATE)
        # e.g., "Denver CO" or "New York NY" -> "Denver Colorado"
        # Only match uppercase 2-letter codes after a capitalized word
        # A word that is already a whole state name ("Maryland MD") is not a city: leaving it
        # keeps the result stable on a second pass.
        pattern = rf'([A-Z][a-z]+)\s+{abbrev}\b'
        text = re.sub(
            pattern,
            lambda m: m.group(0) if m.group(1) in _SINGLE_WORD_STATES else f'{m.group(1)}, {full}',
            text,
        )

    return text


def expand_common_abbreviations(text: str) -> str:
    """Expand common abbreviations."""
    for pattern, replacement in COMMON_ABBREVIATIONS.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


_DIRECTION_LETTERS = {'N': 'North', 'S': 'South', 'E': 'East', 'W': 'West'}
_STREET_SUFFIXES = r'(Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln)'
_HIGHWAYS = r'(I-\d{1,6}|Route\s{1,3}\d{1,6}|Hwy\s{1,3}\d{1,6}|Highway\s{1,3}\d{1,6})'


def expand_directions(text: str) -> str:
    """Expand directional abbreviations in address context."""

    # Handle two-letter directions FIRST (NE, NW, SE, SW) to avoid state abbr conflicts
    # Match at end of address or before punctuation
    text = re.sub(r'\bNE\b(?=\s*$|\s*,|\s*\.)', 'Northeast', text)
    text = re.sub(r'\bNW\b(?=\s*$|\s*,|\s*\.)', 'Northwest', text)
    text = re.sub(r'\bSE\b(?=\s*$|\s*,|\s*\.)', 'Southeast', text)
    text = re.sub(r'\bSW\b(?=\s*$|\s*,|\s*\.)', 'Southwest', text)

    # Direction BEFORE street name: "100 N Main St" -> "100 North Main Street"
    for letter, word in _DIRECTION_LETTERS.items():
        if letter not in text:
            continue
        text = re.sub(rf'{_NUM_START}(\d{{1,9}}\s{{0,3}}){letter}\.?(?=\s+[A-Za-z])', rf'\1{word} ', text)

    # Direction AFTER street suffix: "Main St E" or "Main Street E"
    for letter, word in _DIRECTION_LETTERS.items():
        text = re.sub(rf'{_STREET_SUFFIXES}\s{{1,3}}{letter}\b', rf'\1 {word}', text, flags=re.IGNORECASE)

    # Highway directions: "I-95 N" or "Route 1 S"
    for letter, word in _DIRECTION_LETTERS.items():
        text = re.sub(rf'{_HIGHWAYS}\s{{1,3}}{letter}\b', rf'\1 {word}', text, flags=re.IGNORECASE)

    return text


def normalize_phone_numbers(text: str) -> str:
    """Format phone numbers for natural speech."""
    # Convert (123) 456-7890 or 123-456-7890 to spoken format
    # TTS usually handles this well, but we can help

    def speak_phone(match):
        digits = re.sub(r'\D', '', match.group(0))
        if len(digits) == 10:
            # Format as: "area code 123, 456, 7890"
            return f"{digits[0]} {digits[1]} {digits[2]}, {digits[3]} {digits[4]} {digits[5]}, {digits[6]} {digits[7]} {digits[8]} {digits[9]}"
        return match.group(0)

    # Match common phone formats
    phone_pattern = r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}'
    text = re.sub(phone_pattern, speak_phone, text)

    return text


def normalize_time(text: str) -> str:
    """Normalize time expressions for natural speech."""
    # Convert AM/PM to natural phrases for TTS
    # Morning: 12:00 AM - 11:59 AM -> "in the morning"
    # Afternoon: 12:00 PM - 5:59 PM -> "in the afternoon"
    # Evening: 6:00 PM - 11:59 PM -> "in the evening"

    def get_time_of_day(hour: int, is_pm: bool) -> str:
        """Determine time of day phrase."""
        if not is_pm:  # AM
            if hour == 12:
                return "at night"  # 12 AM = midnight
            return "in the morning"
        else:  # PM
            if hour == 12:
                return "in the afternoon"  # 12 PM = noon
            elif hour < 6:
                return "in the afternoon"
            else:
                return "in the evening"

    def format_minutes(minutes_str: str) -> str:
        """Format minutes for natural speech - '03' becomes 'oh 3', '30' stays '30'."""
        minutes = int(minutes_str)
        if minutes == 0:
            return None  # Will be handled as o'clock
        elif minutes < 10:
            return f"oh {minutes}"  # "03" -> "oh 3"
        else:
            return minutes_str  # "30" stays "30"

    def replace_am_pm(match):
        """Replace AM/PM with time of day phrase."""
        time_part = match.group(1)
        am_pm = match.group(2).upper()
        is_pm = am_pm == 'PM'

        # Extract hour and minutes
        if ':' in time_part:
            hour_str, minutes_str = time_part.split(':')
            hour = int(hour_str)
            formatted_minutes = format_minutes(minutes_str)
            if formatted_minutes is None:
                time_spoken = f"{hour} o'clock"
            else:
                time_spoken = f"{hour} {formatted_minutes}"
        else:
            hour = int(time_part)
            time_spoken = str(hour)

        # Handle 12-hour edge cases
        if hour == 12:
            phrase = get_time_of_day(12, is_pm)
        else:
            phrase = get_time_of_day(hour, is_pm)

        return f"{time_spoken} {phrase}"

    # Handle times with minutes: "10:30 AM" or "10:30AM"
    text = re.sub(r'(\d{1,2}:\d{2})\s{0,3}(AM|PM)\b', replace_am_pm, text, flags=re.IGNORECASE)

    # Handle times without minutes: "8 AM" or "8AM"
    text = re.sub(r'(\d{1,2})\s{0,3}(AM|PM)\b', replace_am_pm, text, flags=re.IGNORECASE)

    # Handle "a.m." and "p.m." formats with minutes
    def replace_am_pm_dot(match):
        time_part = match.group(1)
        period = match.group(2).lower()
        is_pm = period == 'p.m.'

        if ':' in time_part:
            hour_str, minutes_str = time_part.split(':')
            hour = int(hour_str)
            formatted_minutes = format_minutes(minutes_str)
            if formatted_minutes is None:
                time_spoken = f"{hour} o'clock"
            else:
                time_spoken = f"{hour} {formatted_minutes}"
        else:
            hour = int(time_part)
            time_spoken = str(hour)

        phrase = "in the morning" if not is_pm else "in the afternoon"
        return f"{time_spoken} {phrase}"

    text = re.sub(r'(\d{1,2}:\d{2})\s{0,3}(a\.m\.|p\.m\.)', replace_am_pm_dot, text, flags=re.IGNORECASE)
    text = re.sub(r'(\d{1,2})\s{0,3}(a\.m\.|p\.m\.)', replace_am_pm_dot, text, flags=re.IGNORECASE)

    # Convert standalone ":0X" to " oh X" for times without AM/PM
    # "12:03" -> "12 oh 3", but "12:30" stays "12:30"
    def replace_leading_zero_minutes(match):
        hour = match.group(1)
        minute = int(match.group(2))
        return f"{hour} oh {minute}"

    text = re.sub(r'\b(\d{1,2}):0([1-9])\b', replace_leading_zero_minutes, text)

    # Convert ":00" to "o'clock" for on-the-hour times
    # "12:00" -> "12 o'clock", but not "12:30" (leave as-is)
    text = re.sub(r'\b(\d{1,2}):00\b', r"\1 o'clock", text)

    return text


def normalize_timezones(text: str) -> str:
    """Normalize timezone abbreviations for natural speech.

    Converts common timezone abbreviations to full names.
    """
    # Common US timezone abbreviations (order matters - check longer ones first)
    timezone_map = {
        r'\bEST\b': 'Eastern Standard Time',
        r'\bEDT\b': 'Eastern Daylight Time',
        r'\bET\b': 'Eastern Time',
        r'\bCST\b': 'Central Standard Time',
        r'\bCDT\b': 'Central Daylight Time',
        r'\bCT\b': 'Central Time',
        r'\bMST\b': 'Mountain Standard Time',
        r'\bMDT\b': 'Mountain Daylight Time',
        r'\bMT\b': 'Mountain Time',
        r'\bPST\b': 'Pacific Standard Time',
        r'\bPDT\b': 'Pacific Daylight Time',
        r'\bPT\b': 'Pacific Time',
        r'\bUTC\b': 'Coordinated Universal Time',
        r'\bGMT\b': 'Greenwich Mean Time',
    }

    for pattern, replacement in timezone_map.items():
        text = re.sub(pattern, replacement, text)

    return text


def normalize_dates(text: str) -> str:
    """Normalize date expressions for natural speech.

    Handles:
    - Leading zeros in days: "January 06" -> "January 6th"
    - Numeric dates: "01/06/2026" -> "January 6th, 2026"
    """
    # Day number to ordinal mapping
    def day_to_ordinal(day: int) -> str:
        if 11 <= day <= 13:
            return f"{day}th"
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
        return f"{day}{suffix}"

    # Month names
    months = [
        'January', 'February', 'March', 'April', 'May', 'June',
        'July', 'August', 'September', 'October', 'November', 'December'
    ]

    # Handle "Month DD" format with leading zeros: "January 06" -> "January 6th"
    def replace_month_day(match):
        month = match.group(1)
        day = int(match.group(2))  # Remove leading zero
        return f"{month} {day_to_ordinal(day)}"

    month_pattern = r'\b(' + '|'.join(months) + r')\s+0?(\d{1,2})\b'
    text = re.sub(month_pattern, replace_month_day, text, flags=re.IGNORECASE)

    # Handle numeric date formats: "01/06/2026" or "1/6/2026" -> "January 6th, 2026"
    def replace_numeric_date(match):
        month_num = int(match.group(1))
        day = int(match.group(2))
        year = match.group(3)

        if 1 <= month_num <= 12:
            month_name = months[month_num - 1]
            return f"{month_name} {day_to_ordinal(day)}, {year}"
        return match.group(0)  # Return unchanged if invalid

    # MM/DD/YYYY or M/D/YYYY
    text = re.sub(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b', replace_numeric_date, text)

    return text


def _counted_rule(unit_pattern: str, singular: str, plural: str, *, flags: int = 0, tail: str = ''):
    """Build a `<number> <unit>` rewrite that agrees the unit with the number."""
    rx = re.compile(_NUM + _GAP + unit_pattern + tail, flags)

    def apply(text: str) -> str:
        return rx.sub(lambda m: f"{m.group(1)} {_unit(m.group(1), singular, plural)}", text)

    return apply


_I = re.IGNORECASE

# What may follow "45 F": punctuation, whitespace, or a symbol a later rule turns into a space.
_SPACED_DEGREE_END = r'(?=[,.\s$€£&+@#]|$)'

_TEMPERATURE_RULES = (
    _counted_rule(r'°\s{0,3}F\b', 'degree Fahrenheit', 'degrees Fahrenheit', flags=_I),
    _counted_rule(r'°\s{0,3}C\b', 'degree Celsius', 'degrees Celsius', flags=_I),
    # "45 F" / "45 C" after a number, before end or punctuation ("F-150" is left alone)
    _counted_rule(r'(?<=\s)F\b', 'degree Fahrenheit', 'degrees Fahrenheit', tail=_SPACED_DEGREE_END),
    _counted_rule(r'(?<=\s)C\b', 'degree Celsius', 'degrees Celsius', tail=_SPACED_DEGREE_END),
    # a bare degree sign after a number, once the °F / °C rules have run
    _counted_rule(r'°(?![A-Za-z])', 'degree', 'degrees'),
)

_SPEED_RULES = (
    _counted_rule(r'mph\b', 'mile per hour', 'miles per hour', flags=_I),
    _counted_rule(r'(?:km/hr?|kmh|kph)\b', 'kilometer per hour', 'kilometers per hour', flags=_I),
    _counted_rule(r'm/s\b', 'meter per second', 'meters per second'),
    _counted_rule(r'kts?\b', 'knot', 'knots'),
)

_PRESSURE_RULES = (
    _counted_rule(r'hPa\b', 'hectopascal', 'hectopascals', flags=_I),
    _counted_rule(r'in\s{0,3}Hg\b', 'inch of mercury', 'inches of mercury', flags=_I),
    _counted_rule(r'mbar\b', 'millibar', 'millibars', flags=_I),
)

_MEASUREMENT_RULES = (
    # Weight
    _counted_rule(r'lbs?\b', 'pound', 'pounds', flags=_I),
    _counted_rule(r'oz\b', 'ounce', 'ounces', flags=_I),
    _counted_rule(r'kg\b', 'kilogram', 'kilograms', flags=_I),
    _counted_rule(r'g(?!\w)', 'gram', 'grams', flags=_I),
    # Volume
    _counted_rule(r'ml\b', 'milliliter', 'milliliters', flags=_I),
    _counted_rule(r'L\b', 'liter', 'liters'),
    _counted_rule(r'gal\b', 'gallon', 'gallons', flags=_I),
    _counted_rule(r'qt\b', 'quart', 'quarts', flags=_I),
    # Length
    _counted_rule(r'km\b', 'kilometer', 'kilometers', flags=_I),
    _counted_rule(r'cm\b', 'centimeter', 'centimeters', flags=_I),
    _counted_rule(r'mm\b', 'millimeter', 'millimeters', flags=_I),
    _counted_rule(r'mi\b', 'mile', 'miles', flags=_I),
    _counted_rule(r'sq\s{0,3}ft\b', 'square foot', 'square feet', flags=_I),
    _counted_rule(r'ft\b', 'foot', 'feet', flags=_I),
    # Time
    _counted_rule(r'hrs?\b', 'hour', 'hours', flags=_I),
    _counted_rule(r'mins?\b', 'minute', 'minutes', flags=_I),
)

# "in" followed by one of these is a preposition ("6 feet 10 in the evening").
_PREPOSITION_OBJECT = r'(?i:the|a|an|my|your|his|her|its|our|their|this|that|these|those)'
_FEET_INCHES_RE = re.compile(
    r'\b((?i:feet|foot|ft))\s{1,3}(\d{1,3}(?:\.\d{1,3})?)\s{0,3}in\b'
    rf'(?:(\.)(?=\s|$|&)|(?!\.)(?!\s{{1,3}}{_PREPOSITION_OBJECT}\b))'
)
_INCH_PERIOD_RE = re.compile(_NUM + _GAP + r'in\.(?=\s|$|&)')
_INCH_DIMENSION_RE = re.compile(_NUM + _GAP + r'in(?=\s{0,3}[x×]\s{0,3}\d)')
_INCH_AT_END_RE = re.compile(_NUM + _GAP + r'in(?=\s*\Z)')
# A period after "in" stays when it can end the sentence: end of text, or a
# capital letter starts the next one.
_SENTENCE_BOUNDARY_RE = re.compile(r'\s*\Z|\s+[A-Z]')


def _inches(count: str) -> str:
    return f"{count} {_unit(count, 'inch', 'inches')}"


def _inch_period(m: 're.Match[str]') -> str:
    keeps_period = _SENTENCE_BOUNDARY_RE.match(m.string, m.end())
    return _inches(m.group(1)) + ('.' if keeps_period else '')


def normalize_inches(text: str) -> str:
    """Inches written as "in" / "in." after a number, and "5 feet 9 in"."""
    # "in" is a common word: only expand where a number precedes and the shape is unambiguous.
    text = _FEET_INCHES_RE.sub(
        lambda m: f"{'feet' if m.group(1).lower() == 'ft' else m.group(1)} {_inches(m.group(2))}" + (
            '.' if m.group(3) and _SENTENCE_BOUNDARY_RE.match(m.string, m.end()) else ''
        ),
        text,
    )
    text = _INCH_PERIOD_RE.sub(_inch_period, text)
    text = _INCH_DIMENSION_RE.sub(lambda m: _inches(m.group(1)), text)
    text = _INCH_AT_END_RE.sub(lambda m: _inches(m.group(1)), text)
    return text


def normalize_temperature(text: str) -> str:
    """Normalize temperature expressions for natural speech."""
    for rule in _TEMPERATURE_RULES:
        text = rule(text)

    # Match "degrees F" or "degrees C" -> expand to full word
    text = re.sub(r'degrees\s{1,3}F\b', 'degrees Fahrenheit', text, flags=re.IGNORECASE)
    text = re.sub(r'degrees\s{1,3}C\b', 'degrees Celsius', text, flags=re.IGNORECASE)

    return text


_PERCENT_RE = re.compile(_NUM_START + r'(\d{1,9}(?:\.\d{1,9})?)\s{0,3}%')


def normalize_percentages(text: str) -> str:
    """Normalize percentage expressions."""
    # "50%" -> "50 percent"
    return _PERCENT_RE.sub(r'\1 percent', text)


_SCALE_SUFFIXES = {'k': 'thousand', 'm': 'million', 'b': 'billion', 't': 'trillion'}
_SCALE_WORDS = ('thousand', 'million', 'billion', 'trillion')

# 1-9 plain digits, or comma thousands groups ("1,000", "12,500", "1,000,000"); optional
# cents; optional scale ("5 million", "1.5B", "300K").
_AMOUNT = (
    r'(\d{1,3}(?:,\d{3}){1,3}|\d{1,9})(?:\.(\d{1,2}))?'
    r'(?:\s{1,3}(' + '|'.join(_SCALE_WORDS) + r')\b|([KMBTkmbt])(?![A-Za-z0-9]))?(?!\d)'
)
_DOLLARS_RE = re.compile(r'(?<![$€£])\$' + _AMOUNT, re.IGNORECASE)
_EUROS_RE = re.compile(r'(?<![$€£])€' + _AMOUNT, re.IGNORECASE)
_POUNDS_RE = re.compile(r'(?<![$€£])£' + _AMOUNT, re.IGNORECASE)
_ATTACHED_AFTER = re.compile(r'[A-Za-z0-9]|[$€£#]\d')
_ATTACHED_AMOUNT_AFTER = re.compile(r'\d|[$€£#]\d')


def _pad(match, words: str, *, letters_after: bool = True) -> str:
    """Space a rewritten token off a letter or digit before it, or after it a letter, digit
    or another amount ("5$1.5", "€5€6", "#1#2"), so the words never run together."""
    before = match.string[match.start() - 1:match.start()] if match.start() else ''
    left = ' ' if before.isalnum() else ''
    attached = _ATTACHED_AFTER if letters_after else _ATTACHED_AMOUNT_AFTER
    right = ' ' if attached.match(match.string, match.end()) else ''
    return left + words + right


def _money_words(match, singular: str, plural: str, *, cents_unit: bool) -> str:
    whole, cents, scale_word, scale_suffix = match.groups()
    scale = (scale_word or _SCALE_SUFFIXES.get((scale_suffix or '').lower(), '')).lower()
    if scale:
        amount = f"{whole}.{cents}" if cents is not None else whole
        return _pad(match, f"{amount} {scale} {plural}")
    whole_value = int(whole.replace(',', ''))
    if not cents_unit:
        amount = f"{whole}.{cents}" if cents is not None else whole
        return _pad(match, f"{amount} {_unit(amount, singular, plural)}")
    cent_value = int(cents.ljust(2, '0')) if cents else 0
    dollar_words = f"{whole} {_unit(str(whole_value), singular, plural)}"
    cent_words = f"{cent_value} {_unit(str(cent_value), 'cent', 'cents')}"
    if cent_value == 0:
        return _pad(match, dollar_words)
    if whole_value == 0:
        return _pad(match, cent_words)
    return _pad(match, f"{dollar_words} and {cent_words}")


def normalize_currency(text: str) -> str:
    """Normalize currency expressions for natural speech."""
    # Price ratings FIRST (before dollar amounts), only as a standalone token:
    # "$$$$" -> "very expensive", "$$$" -> "expensive", "$$" -> "moderately priced"
    text = re.sub(r'(?<![\w$])\${4}(?![\w$])', 'very expensive', text)
    text = re.sub(r'(?<![\w$])\${3}(?![\w$])', 'expensive', text)
    text = re.sub(r'(?<![\w$])\${2}(?![\w$])', 'moderately priced', text)
    # Single $ only when standalone (not before a number) for price rating
    text = re.sub(r'(?<![^\s&])\$(?!\d)(?=\s|$|,|\.|&)', 'budget-friendly', text)

    text = _DOLLARS_RE.sub(lambda m: _money_words(m, 'dollar', 'dollars', cents_unit=True), text)
    text = _EUROS_RE.sub(lambda m: _money_words(m, 'euro', 'euros', cents_unit=False), text)
    text = _POUNDS_RE.sub(lambda m: _money_words(m, 'pound', 'pounds', cents_unit=False), text)

    return text


def normalize_speeds(text: str) -> str:
    """Speeds, spaced or not: "25mph", "15 km/h", "10 m/s", "20 kts"."""
    for rule in _SPEED_RULES:
        text = rule(text)
    return text


def normalize_pressure(text: str) -> str:
    """Barometric pressure: "1013 hPa", "29.92 inHg", "1008 mbar"."""
    for rule in _PRESSURE_RULES:
        text = rule(text)
    return text


def normalize_measurements(text: str) -> str:
    """Normalize measurement units for natural speech."""
    for rule in _MEASUREMENT_RULES:
        text = rule(text)
    return normalize_inches(text)


def normalize_scores(text: str) -> str:
    """Normalize sports scores for natural speech.

    Converts "28-14" to "28 to 14" for game scores.
    Only matches reasonable score patterns (0-199 range).
    """
    # Match score pattern: number-number where both are reasonable scores
    # Avoid matching years (2023-2024), zip extensions, phone numbers
    # Score pattern: 1-3 digit numbers, typically less than 200

    def replace_score(match):
        score1 = match.group(1)
        score2 = match.group(2)
        # Only treat as score if both numbers are reasonable (0-199)
        if int(score1) < 200 and int(score2) < 200:
            return f"{score1} to {score2}"
        return match.group(0)

    # Match patterns like "28-14", "7-3", "110-98"
    # Require word boundary or space before, avoid matching in middle of larger numbers
    # Negative lookbehind for digits/dash, negative lookahead for digits/dash
    text = re.sub(r'(?<![0-9-])(\d{1,3})-(\d{1,3})(?![0-9-])', replace_score, text)

    return text


def normalize_sports_records(text: str) -> str:
    """Normalize sports team records for natural speech.

    Converts team records like "4-13" or "4 - 13" to "4 and 13" or "4 wins and 13 losses".
    Handles patterns with spaces around the dash.
    """
    # Pattern for records with optional spaces around dash: "4-13", "4 - 13", "4- 13"
    # These are typically team records (wins-losses) not game scores

    # Win-loss-tie: "10-5-1" (single-digit ties keep short dates like "10-5-24" out)
    text = re.sub(
        r'(?<![\d-])(\d{1,2})-(\d{1,2})-(\d)(?![\d-])',
        lambda m: f"{m.group(1)} {_unit(m.group(1), 'win', 'wins')}, "
                  f"{m.group(2)} {_unit(m.group(2), 'loss', 'losses')} and "
                  f"{m.group(3)} {_unit(m.group(3), 'tie', 'ties')}",
        text,
    )

    # Match records with spaces around dash: "4 - 13", "10 - 5". The second number is a
    # lookahead so "2 - 2 - 1" settles in one pass (a consumed number can't start the next match).
    text = re.sub(r'\b(\d{1,2})\s+-\s+(?=\d{1,2}\b)', r'\1 and ', text)

    # Also match "record of X-Y" (or "is" / "was" / "stands at" / "at") and "X-Y record" patterns
    text = re.sub(r'record\s+(?:(of|is|was|stands\s+at|at)\s+)?(\d{1,2})-(\d{1,2})',
                  lambda m: f"record {m.group(1) or 'of'} {m.group(2)} wins and {m.group(3)} losses",
                  text, flags=re.IGNORECASE)
    text = re.sub(r'(\d{1,2})-(\d{1,2})\s+record',
                  lambda m: f"{m.group(1)} and {m.group(2)} record", text, flags=re.IGNORECASE)

    return text


def normalize_symbols(text: str) -> str:
    """Normalize common symbols for speech."""
    # "#1" -> "number 1"
    text = re.sub(
        r'#(?=[$€£]?\d)',
        lambda m: (' ' if m.start() and m.string[m.start() - 1].isalnum() else '') + 'number ',
        text,
    )

    # "No. 1", "no 1", "No 1" -> "number 1" (ordinal/ranking context)
    # Must be followed by a number to avoid matching "no" in other contexts
    text = re.sub(
        r'(?<![A-Za-z])[Nn]o\.?\s*(?=[$€£]?\d)',
        lambda m: (' ' if m.start() and m.string[m.start() - 1].isdigit() else '') + 'number ',
        text,
    )

    # "&" -> "and"
    text = re.sub(r'\s*&\s*', ' and ', text)

    # "@" in non-email context -> "at"
    # Skip if it looks like an email. Runs before "+" so that "@ +" settles in one pass.
    text = re.sub(r'(?<!\S)@(?=\s)', 'at ', text)

    # "+" between words -> "plus" (but not in phone numbers)
    # Lookarounds, not captures: "1 + 2 + 3" must settle in one pass.
    # Currency, degree and percent signs count as word characters here: later rules turn them into words.
    text = re.sub(r'(?<=[\w$€£°%.])\s*\+\s*(?=[\w$€£°%])', ' plus ', text)

    return text


def normalize_ordinals(text: str) -> str:
    """Ensure ordinals are properly formatted for TTS."""
    # Most TTS handles 1st, 2nd, 3rd well, but let's ensure consistency
    # "1st" -> "first", "2nd" -> "second", etc. for small numbers
    ordinal_map = {
        '1st': 'first', '2nd': 'second', '3rd': 'third',
        '4th': 'fourth', '5th': 'fifth', '6th': 'sixth',
        '7th': 'seventh', '8th': 'eighth', '9th': 'ninth',
        '10th': 'tenth', '11th': 'eleventh', '12th': 'twelfth',
    }
    for abbr, full in ordinal_map.items():
        text = re.sub(rf'\b{abbr}\b', full, text, flags=re.IGNORECASE)

    return text


def normalize_zip_codes(text: str) -> str:
    """Normalize ZIP codes to be spoken as individual digits.

    e.g., "21201" -> "2 1 2 0 1"
    e.g., "21201-1234" -> "2 1 2 0 1, 1 2 3 4"
    """
    def expand_zip(match):
        """Convert ZIP code digits to space-separated form."""
        zip_main = match.group(1)
        zip_ext = match.group(2) if match.group(2) else None

        # Convert main ZIP to individual digits
        spoken_main = ' '.join(zip_main)

        if zip_ext:
            # Convert extension to individual digits
            spoken_ext = ' '.join(zip_ext)
            return f"{spoken_main}, {spoken_ext}"
        return spoken_main

    # Match ZIP codes that appear after state names (most reliable context)
    # Pattern: state name + space + 5 digits, optionally with -4 extension
    states = '|'.join(STATE_ABBREVIATIONS.values())
    pattern = rf'(?:{states})\s+(\d{{5}})(?:-(\d{{4}}))?'
    text = re.sub(pattern, lambda m: f"{m.group(0).rsplit(' ', 1)[0]} {expand_zip(m)}", text, flags=re.IGNORECASE)

    # Also match ZIP codes at end of text or before punctuation (after address context)
    # Look for: comma + space + 5 digits at end or before period/comma
    text = re.sub(r',\s*(\d{5})(?:-(\d{4}))?(?=\s*[.,]|\s*$)',
                  lambda m: ', ' + expand_zip(m), text)

    return text


def strip_emojis(text: str) -> str:
    """Remove emojis from text for TTS output.

    Emojis cause pronunciation issues in TTS - they're either read as
    "grinning face" or cause weird pauses. Strip them entirely.
    """
    # Unicode ranges for common emoji blocks:
    # - Emoticons: U+1F600-U+1F64F
    # - Misc Symbols: U+1F300-U+1F5FF
    # - Transport: U+1F680-U+1F6FF
    # - Supplemental: U+1F900-U+1F9FF
    # - Dingbats: U+2700-U+27BF
    # - Misc Symbols: U+2600-U+26FF
    # - Extended: U+1FA00-U+1FAFF

    # Comprehensive emoji pattern
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F700-\U0001F77F"  # alchemical symbols
        "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
        "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
        "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
        "\U0001FA00-\U0001FA6F"  # Chess Symbols
        "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
        "\U00002702-\U000027B0"  # Dingbats
        "\U00002600-\U000026FF"  # Misc symbols
        "\U00002300-\U000023FF"  # Misc Technical
        "\U00002B50-\U00002B55"  # Stars
        "\U0000231A-\U0000231B"  # Watch/hourglass
        "\U0000FE00-\U0000FE0F"  # Variation Selectors
        "\U0000200D"             # Zero Width Joiner (used in composed emoji)
        "]+",
        flags=re.UNICODE
    )

    # Remove emojis
    text = emoji_pattern.sub('', text)

    # Clean up any double spaces left behind
    text = re.sub(r'\s+', ' ', text).strip()

    return text


def strip_urls(text: str) -> str:
    """Remove URLs and markdown links from text for TTS output.

    URLs are not helpful when spoken aloud. This removes:
    - Full URLs: https://example.com/path
    - Markdown links: [text](url) - keeps the text, removes the URL
    - Bare URLs: http://... or www...
    """
    # First, extract text from markdown links: [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)

    # Remove standalone URLs (http, https, ftp)
    text = re.sub(r'https?://[^\s<>"{}|\\^`\[\]]+', '', text)
    text = re.sub(r'ftp://[^\s<>"{}|\\^`\[\]]+', '', text)

    # Remove www URLs without protocol
    text = re.sub(r'\bwww\.[^\s<>"{}|\\^`\[\]]+', '', text)

    # Clean up any leftover parentheses from removed links
    text = re.sub(r'\(\s*\)', '', text)

    # Clean up multiple spaces and trim
    text = re.sub(r'\s+', ' ', text).strip()

    return text


def normalize_for_tts(text: str) -> str:
    """
    Main normalization function for TTS output.

    Expands abbreviations and formats text for natural speech synthesis.

    Args:
        text: Raw text to normalize

    Returns:
        Normalized text suitable for TTS
    """
    if not text:
        return text

    # FIRST: Strip emojis and URLs before any other processing
    text = strip_emojis(text)
    text = strip_urls(text)

    # Apply normalizations in order (most specific first)
    text = normalize_time(text)  # AM/PM -> in the morning/afternoon/evening, :00 -> o'clock
    text = normalize_timezones(text)  # ET -> Eastern Time
    text = normalize_dates(text)  # January 06 -> January 6th
    text = normalize_symbols(text)  # & -> and, # -> number (before every digit rule: "no.2 C" settles)
    text = normalize_temperature(text)  # 45 F -> 45 degrees Fahrenheit
    text = normalize_percentages(text)  # 50% -> 50 percent
    text = normalize_currency(text)  # $10 -> 10 dollars
    text = normalize_speeds(text)  # 25mph -> 25 miles per hour
    text = normalize_pressure(text)  # 1013 hPa -> 1013 hectopascals
    text = normalize_measurements(text)  # 5 lbs -> 5 pounds
    text = normalize_sports_records(text)  # 4 - 13 -> 4 and 13 (team records)
    text = normalize_scores(text)  # 28-14 -> 28 to 14 (game scores)
    text = normalize_ordinals(text)  # 1st -> first
    text = expand_common_abbreviations(text)
    text = expand_directions(text)
    text = expand_street_abbreviations(text)
    text = expand_state_abbreviations(text)
    # Disabled: zip code spacing makes TTS sound choppy ("2 1 2 2 4")
    # Piper TTS handles 5-digit zip codes naturally without intervention
    # text = normalize_zip_codes(text)

    # Optional: normalize phone numbers (can make them sound robotic)
    # text = normalize_phone_numbers(text)

    # Clean up any double spaces
    text = re.sub(r'\s+', ' ', text).strip()

    return text
