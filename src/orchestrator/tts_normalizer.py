"""
TTS Text Normalizer for Athena Voice Output

Expands abbreviations and formats text for natural speech synthesis.
Handles street names, state abbreviations, and common acronyms.
"""

import re
from typing import Dict

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
    r'\bkph\b': 'kilometers per hour',
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


def expand_state_abbreviations(text: str) -> str:
    """Expand US state abbreviations."""
    # Pattern: comma + space + two-letter state code (optionally followed by ZIP)
    # e.g., "Baltimore, MD" -> "Baltimore, Maryland"
    # e.g., "Baltimore, MD 21201" -> "Baltimore, Maryland 21201"
    # Also: "Baltimore MD" -> "Baltimore Maryland" (no comma)

    for abbrev, full in STATE_ABBREVIATIONS.items():
        # Match state abbreviation after comma or at word boundary
        # Avoid matching in the middle of words
        pattern = rf',\s*{abbrev}\b'
        replacement = f', {full}'
        text = re.sub(pattern, replacement, text)

        # Match standalone state abbreviations (less aggressive)
        pattern = rf'\b{abbrev}\b(?=\s*\d{{5}}|\s*$|\s*[,.])'
        text = re.sub(pattern, full, text)

        # Match state abbreviation after city name (word + space + STATE)
        # e.g., "Baltimore MD" or "New York NY" -> "Baltimore Maryland"
        # Only match uppercase 2-letter codes after a capitalized word
        pattern = rf'([A-Z][a-z]+)\s+{abbrev}\b'
        text = re.sub(pattern, rf'\1, {full}', text)

    return text


def expand_common_abbreviations(text: str) -> str:
    """Expand common abbreviations."""
    for pattern, replacement in COMMON_ABBREVIATIONS.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def expand_directions(text: str) -> str:
    """Expand directional abbreviations in address context."""

    # Handle two-letter directions FIRST (NE, NW, SE, SW) to avoid state abbr conflicts
    # Match at end of address or before punctuation
    text = re.sub(r'\bNE\b(?=\s*$|\s*,|\s*\.)', 'Northeast', text)
    text = re.sub(r'\bNW\b(?=\s*$|\s*,|\s*\.)', 'Northwest', text)
    text = re.sub(r'\bSE\b(?=\s*$|\s*,|\s*\.)', 'Southeast', text)
    text = re.sub(r'\bSW\b(?=\s*$|\s*,|\s*\.)', 'Southwest', text)

    # Direction BEFORE street name: "100 N Main St" -> "100 North Main Street"
    text = re.sub(r'(\d+\s*)N\.?(?=\s+[A-Za-z])', r'\1North ', text)
    text = re.sub(r'(\d+\s*)S\.?(?=\s+[A-Za-z])', r'\1South ', text)
    text = re.sub(r'(\d+\s*)E\.?(?=\s+[A-Za-z])', r'\1East ', text)
    text = re.sub(r'(\d+\s*)W\.?(?=\s+[A-Za-z])', r'\1West ', text)

    # Direction AFTER street suffix: "Main St E" or "Main Street E"
    text = re.sub(r'(Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln)\s+N\b', r'\1 North', text, flags=re.IGNORECASE)
    text = re.sub(r'(Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln)\s+S\b', r'\1 South', text, flags=re.IGNORECASE)
    text = re.sub(r'(Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln)\s+E\b', r'\1 East', text, flags=re.IGNORECASE)
    text = re.sub(r'(Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln)\s+W\b', r'\1 West', text, flags=re.IGNORECASE)

    # Highway directions: "I-95 N" or "Route 1 S"
    text = re.sub(r'(I-\d+|Route\s+\d+|Hwy\s+\d+|Highway\s+\d+)\s+N\b', r'\1 North', text, flags=re.IGNORECASE)
    text = re.sub(r'(I-\d+|Route\s+\d+|Hwy\s+\d+|Highway\s+\d+)\s+S\b', r'\1 South', text, flags=re.IGNORECASE)
    text = re.sub(r'(I-\d+|Route\s+\d+|Hwy\s+\d+|Highway\s+\d+)\s+E\b', r'\1 East', text, flags=re.IGNORECASE)
    text = re.sub(r'(I-\d+|Route\s+\d+|Hwy\s+\d+|Highway\s+\d+)\s+W\b', r'\1 West', text, flags=re.IGNORECASE)

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
    text = re.sub(r'(\d{1,2}:\d{2})\s*(AM|PM)\b', replace_am_pm, text, flags=re.IGNORECASE)

    # Handle times without minutes: "8 AM" or "8AM"
    text = re.sub(r'(\d{1,2})\s*(AM|PM)\b', replace_am_pm, text, flags=re.IGNORECASE)

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

    text = re.sub(r'(\d{1,2}:\d{2})\s*(a\.m\.|p\.m\.)', replace_am_pm_dot, text, flags=re.IGNORECASE)
    text = re.sub(r'(\d{1,2})\s*(a\.m\.|p\.m\.)', replace_am_pm_dot, text, flags=re.IGNORECASE)

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


def normalize_temperature(text: str) -> str:
    """Normalize temperature expressions for natural speech."""
    # Convert "45°F" or "45 F" or "45F" to "45 degrees Fahrenheit"
    # Convert "10°C" or "10 C" or "10C" to "10 degrees Celsius"

    # Match temperature with degree symbol: 45°F, 45° F
    text = re.sub(r'(\d+)\s*°\s*F\b', r'\1 degrees Fahrenheit', text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+)\s*°\s*C\b', r'\1 degrees Celsius', text, flags=re.IGNORECASE)

    # Match temperature without degree symbol but with space: "45 F" (after a number, before end/punctuation)
    # Be careful not to match things like "F-150" or other uses of F
    text = re.sub(r'(\d+)\s+F\b(?=\s*[,.\s]|$)', r'\1 degrees Fahrenheit', text)
    text = re.sub(r'(\d+)\s+C\b(?=\s*[,.\s]|$)', r'\1 degrees Celsius', text)

    # Match "degrees F" or "degrees C" -> expand to full word
    text = re.sub(r'degrees\s+F\b', 'degrees Fahrenheit', text, flags=re.IGNORECASE)
    text = re.sub(r'degrees\s+C\b', 'degrees Celsius', text, flags=re.IGNORECASE)

    return text


def normalize_percentages(text: str) -> str:
    """Normalize percentage expressions."""
    # "50%" -> "50 percent"
    text = re.sub(r'(\d+(?:\.\d+)?)\s*%', r'\1 percent', text)
    return text


def normalize_currency(text: str) -> str:
    """Normalize currency expressions for natural speech."""
    # Handle restaurant price ratings FIRST (before dollar amounts)
    # "$$$$" -> "very expensive", "$$$" -> "expensive", "$$" -> "moderate", "$" -> "budget-friendly"
    text = re.sub(r'\$\$\$\$', 'very expensive', text)
    text = re.sub(r'\$\$\$', 'expensive', text)
    text = re.sub(r'\$\$', 'moderately priced', text)
    # Single $ only when standalone (not before a number) for price rating
    text = re.sub(r'(?<!\S)\$(?!\d)(?=\s|$|,|\.)', 'budget-friendly', text)

    # "$10" -> "10 dollars", "$10.50" -> "10 dollars and 50 cents"
    # Handle dollar amounts
    def expand_dollars(match):
        amount = match.group(1)
        if '.' in amount:
            dollars, cents = amount.split('.')
            cents = cents.ljust(2, '0')[:2]  # Ensure 2 digits
            if int(cents) == 0:
                return f"{dollars} dollars"
            elif int(dollars) == 0:
                return f"{cents} cents"
            else:
                return f"{dollars} dollars and {cents} cents"
        return f"{amount} dollars"

    text = re.sub(r'\$(\d+(?:\.\d{1,2})?)', expand_dollars, text)

    # Handle euro amounts
    text = re.sub(r'€(\d+(?:\.\d{1,2})?)', r'\1 euros', text)

    # Handle pound amounts
    text = re.sub(r'£(\d+(?:\.\d{1,2})?)', r'\1 pounds', text)

    return text


def normalize_measurements(text: str) -> str:
    """Normalize measurement units for natural speech."""
    # Weight
    text = re.sub(r'(\d+(?:\.\d+)?)\s*lbs?\b', r'\1 pounds', text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+(?:\.\d+)?)\s*oz\b', r'\1 ounces', text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+(?:\.\d+)?)\s*kg\b', r'\1 kilograms', text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+(?:\.\d+)?)\s*g\b(?!\w)', r'\1 grams', text, flags=re.IGNORECASE)

    # Volume
    text = re.sub(r'(\d+(?:\.\d+)?)\s*ml\b', r'\1 milliliters', text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+(?:\.\d+)?)\s*L\b', r'\1 liters', text)
    text = re.sub(r'(\d+(?:\.\d+)?)\s*gal\b', r'\1 gallons', text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+(?:\.\d+)?)\s*qt\b', r'\1 quarts', text, flags=re.IGNORECASE)

    # Length
    text = re.sub(r'(\d+(?:\.\d+)?)\s*km\b', r'\1 kilometers', text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+(?:\.\d+)?)\s*cm\b', r'\1 centimeters', text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+(?:\.\d+)?)\s*mm\b', r'\1 millimeters', text, flags=re.IGNORECASE)
    # Be careful: "in" is a common word, only match "in." with period or at end of sentence
    # Don't match "1 in the" - that's preposition, not inches
    text = re.sub(r'(\d+(?:\.\d+)?)\s*in\.(?=\s|$)', r'\1 inches', text)  # "5 in." with period
    text = re.sub(r'(\d+(?:\.\d+)?)\s*in(?=\s*[x×]\s*\d)', r'\1 inches', text)  # "5 in x 3" dimensions

    return text


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

    def replace_record(match):
        wins = match.group(1)
        losses = match.group(2)
        # For small numbers typical of win-loss records (0-99)
        if int(wins) < 100 and int(losses) < 100:
            return f"{wins} and {losses}"
        return match.group(0)

    # Match records with spaces around dash: "4 - 13", "10 - 5"
    # This pattern specifically targets records (spaces around dash are common in formatted output)
    text = re.sub(r'\b(\d{1,2})\s+-\s+(\d{1,2})\b', replace_record, text)

    # Also match "record of X-Y" or "X-Y record" patterns
    text = re.sub(r'record\s+(?:of\s+)?(\d{1,2})-(\d{1,2})',
                  lambda m: f"record of {m.group(1)} wins and {m.group(2)} losses", text, flags=re.IGNORECASE)
    text = re.sub(r'(\d{1,2})-(\d{1,2})\s+record',
                  lambda m: f"{m.group(1)} and {m.group(2)} record", text, flags=re.IGNORECASE)

    return text


def normalize_symbols(text: str) -> str:
    """Normalize common symbols for speech."""
    # "#1" -> "number 1"
    text = re.sub(r'#(\d+)', r'number \1', text)

    # "No. 1", "no 1", "No 1" -> "number 1" (ordinal/ranking context)
    # Must be followed by a number to avoid matching "no" in other contexts
    text = re.sub(r'\b[Nn]o\.?\s*(\d+)', r'number \1', text)

    # "&" -> "and"
    text = re.sub(r'\s*&\s*', ' and ', text)

    # "+" between words -> "plus" (but not in phone numbers)
    text = re.sub(r'(\w)\s*\+\s*(\w)', r'\1 plus \2', text)

    # "@" in non-email context -> "at"
    # Skip if it looks like an email
    text = re.sub(r'(?<!\S)@(?=\s)', 'at ', text)

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
    text = normalize_temperature(text)  # 45 F -> 45 degrees Fahrenheit
    text = normalize_percentages(text)  # 50% -> 50 percent
    text = normalize_currency(text)  # $10 -> 10 dollars
    text = normalize_measurements(text)  # 5 lbs -> 5 pounds
    text = normalize_sports_records(text)  # 4 - 13 -> 4 and 13 (team records)
    text = normalize_scores(text)  # 28-14 -> 28 to 14 (game scores)
    text = normalize_symbols(text)  # & -> and, # -> number
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


# Quick test
if __name__ == "__main__":
    test_cases = [
        "Samos Greek Island Grill at 3362 Harford Rd, Baltimore, MD",
        "Try Ikaros at 4805 Eastern Ave, Baltimore, MD 21224",
        "Dr. Smith lives at 100 N Main St, Towson, MD",
        "The restaurant is approx. 10 mins away on Hwy 95",
        "Located at 500 E Pratt St, Baltimore, MD 21202",
        # Time tests
        "The meeting is at 10:30 AM tomorrow",
        "Store opens at 9AM and closes at 8PM",
        "The flight departs at 6:45 PM",
        # Temperature tests
        "Currently 45°F with a high of 52 F",
        "It's 10°C outside, feeling like 8 C",
        "Temperatures will reach 75 degrees F today",
        # Percentage tests
        "There's a 70% chance of rain",
        "Battery at 85%",
        # Currency tests
        "The meal costs $25.50",
        "Price is $10 per person",
        # Measurement tests
        "The package weighs 5 lbs",
        "Add 250ml of water",
        "It's about 10 km away",
        # Symbol tests
        "Ben & Jerry's ice cream",
        "Ranked #1 in the city",
        # Ordinal tests
        "This is the 1st time and 2nd attempt",
        # Restaurant price rating tests
        "The restaurant is rated $$ for price",
        "This place is $$$ - expensive but worth it",
        "Budget option: $ rating",
        "Fine dining at $$$$ prices",
        # ZIP code tests
        "Located at 100 Main St, Baltimore, MD 21201",
        "Address: 500 Pratt St, Baltimore, Maryland 21202",
        "Visit us at 123 Oak Ave, Towson, MD 21204-5678",
        # Sports score tests
        "The Ravens won 28-14 against the Steelers",
        "Final score was 7-3 in a defensive battle",
        "Lakers beat the Celtics 110-98",
        "The game ended 0-0 in regulation",
        # Should NOT be converted (not scores)
        "Call 410-555-1234 for reservations",  # Phone number
        "The 2023-2024 season was great",  # Year range
        "ZIP: 21201-5678",  # ZIP extension
        # Sports records (win-loss with spaces around dash)
        "The team has a 4 - 13 record this season",
        "Currently sitting at 10 - 5 in the standings",
        "They're 0 - 3 on the road",
        "With a record of 15-2, they lead the division",
        # State abbreviations without comma
        "Baltimore MD is a great city",
        "The weather in Philadelphia PA is nice",
        "Visit New York NY this summer",
        # Timezone abbreviations
        "The game starts at 12:00 ET",
        "Kickoff is at 8:30 PM EST",
        "Meeting at 3:00 PT tomorrow",
        "Flight departs at 7:00 CST",
        # O'clock times
        "Arrives at 5:00 today",
        "Store opens at 9:00 and closes at 10:00",
        # "No." / "no" ordinal patterns (must convert to "number")
        "The Eagles are the no 1 seed",
        "He is ranked No. 1 in the world",
        "The team holds the No 2 spot",
        "Currently no 3 in the standings",
        # URL stripping tests
        "Check out https://example.com/page for more info",
        "Visit [our website](https://example.com) today",
        "See www.example.com for details",
        "More info at https://sports.yahoo.com/nfl/standings",
    ]

    print("TTS Normalization Tests:")
    print("=" * 60)
    for test in test_cases:
        normalized = normalize_for_tts(test)
        print(f"IN:  {test}")
        print(f"OUT: {normalized}")
        print("-" * 60)
