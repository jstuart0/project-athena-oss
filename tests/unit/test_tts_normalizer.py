"""shared.tts_normalizer: unit expansion, count agreement, idempotence, linear time.

Every group is a module-level list with a floor assert (so a silently emptied
table fails) and a named member that carries the group's headline behaviour.
`CORPUS` is every input and expected output in every group; the output-channel
tests render it twice to prove a double render equals a single one.
"""
from __future__ import annotations

import random
import time

import pytest

from shared.tts_normalizer import normalize_for_tts as n

# --- Units, spaced and unspaced ---------------------------------------------

# (token, singular, plural)
UNIT_TOKENS = [
    ("mph", "mile per hour", "miles per hour"),
    ("km/h", "kilometer per hour", "kilometers per hour"),
    ("kmh", "kilometer per hour", "kilometers per hour"),
    ("kph", "kilometer per hour", "kilometers per hour"),
    ("m/s", "meter per second", "meters per second"),
    ("kt", "knot", "knots"),
    ("kts", "knot", "knots"),
    ("hPa", "hectopascal", "hectopascals"),
    ("inHg", "inch of mercury", "inches of mercury"),
    ("mbar", "millibar", "millibars"),
    ("lb", "pound", "pounds"),
    ("lbs", "pound", "pounds"),
    ("oz", "ounce", "ounces"),
    ("kg", "kilogram", "kilograms"),
    ("g", "gram", "grams"),
    ("ml", "milliliter", "milliliters"),
    ("L", "liter", "liters"),
    ("gal", "gallon", "gallons"),
    ("qt", "quart", "quarts"),
    ("km", "kilometer", "kilometers"),
    ("cm", "centimeter", "centimeters"),
    ("mm", "millimeter", "millimeters"),
    ("mi", "mile", "miles"),
    ("ft", "foot", "feet"),
    ("hr", "hour", "hours"),
    ("hrs", "hour", "hours"),
    ("min", "minute", "minutes"),
    ("mins", "minute", "minutes"),
    ("°F", "degree Fahrenheit", "degrees Fahrenheit"),
    ("°C", "degree Celsius", "degrees Celsius"),
    ("°", "degree", "degrees"),
]
UNIT_CASES = [
    (f"{count}{gap}{token}", f"{count} {singular if count == '1' else plural}")
    for token, singular, plural in UNIT_TOKENS
    for count in ("1", "5")
    for gap in ("", " ")
]
UNIT_CASES += [
    ("Winds 25mph.", "Winds 25 miles per hour."),
    ("29.92 in Hg", "29.92 inches of mercury"),
    ("5 sq ft", "5 square feet"),
    ("1 sq ft", "1 square foot"),
    ("5in.", "5 inches."),
    ("5 in.", "5 inches."),
    ("5 in x 3", "5 inches x 3"),
]
assert len(UNIT_CASES) >= 120

# --- Gaps measured at the base commit ---------------------------------------

GAP_CASES = [
    ("Winds 25mph.", "Winds 25 miles per hour."),
    ("15 km/h", "15 kilometers per hour"),
    ("10 m/s", "10 meters per second"),
    ("1013 hPa", "1013 hectopascals"),
    ("29.92 inHg", "29.92 inches of mercury"),
    ("It is 72°.", "It is 72 degrees."),
    ("current: 70°, target: 72°", "current: 70 degrees, target: 72 degrees"),
    ("5 ft 9 in", "5 feet 9 inches"),
    ("6 ft 2 in tall", "6 feet 2 inches tall"),
    ("Rain 0.5 in.", "Rain 0.5 inches."),
    ("Rain 2 in. The game", "Rain 2 inches. The game"),
    ("20 kts", "20 knots"),
    ("3 mbar", "3 millibars"),
    ("He is 6 ft 2 in. Next", "He is 6 feet 2 inches. Next"),
    (
        "Winds 25mph, 15 km/h, 10 m/s, 1013 hPa, 29.92 inHg, it is 72°, 1 mi away, 5 ft 9 in.",
        "Winds 25 miles per hour, 15 kilometers per hour, 10 meters per second, "
        "1013 hectopascals, 29.92 inches of mercury, it is 72 degrees, 1 mile away, 5 feet 9 inches.",
    ),
]
assert len(GAP_CASES) >= 15

# --- False positives: words that look like units ----------------------------

FALSE_POSITIVE_CASES = [
    ("in 5 minutes", "in 5 minutes"),
    ("I'm 5 m away", "I'm 5 m away"),
    ("Room 5 in Building B", "Room 5 in Building B"),
    ("Route 66", "Route 66"),
    ("I live in Sydney", "I live in Sydney"),
    ("in the morning", "in the morning"),
    ("F-150", "F-150"),
    ("Model 3", "Model 3"),
    ("Studio 54", "Studio 54"),
    ("Apollo 13", "Apollo 13"),
    ("Catch-22", "Catch-22"),
    ("m", "m"),
    ("in", "in"),
    ("the letter m and the word in", "the letter m and the word in"),
    ("Dr. Smith", "Doctor Smith"),
    ("Main St", "Main Street"),
    ("Sweet 16", "Sweet 16"),
    ("I will be there in 5 minutes", "I will be there in 5 minutes"),
    ("turn left in 500 feet", "turn left in 500 feet"),
    ("she came in 2nd", "she came in second"),
    ("Is it 5 in?", "Is it 5 in?"),
    ("We will be there in 5", "We will be there in 5"),
    ("10-5-24", "10-5-24"),
    ("2024-10-05", "2024-10-05"),
    ("Email jo.smith@example.com today", "Email jo.smith@example.com today"),
    ("I will be there in 5 and call", "I will be there in 5 and call"),
]
assert len(FALSE_POSITIVE_CASES) >= 22

# --- Plurals: only exactly 1 is singular -------------------------------------

PLURAL_CASES = [
    ("1 mile", "1 mile"),
    ("2 miles", "2 miles"),
    ("1 mi", "1 mile"),
    ("1 mph", "1 mile per hour"),
    ("2 mph", "2 miles per hour"),
    ("1 inch", "1 inch"),
    ("3 inches", "3 inches"),
    ("1 in.", "1 inch."),
    ("3 in.", "3 inches."),
    ("1 foot", "1 foot"),
    ("1 ft", "1 foot"),
    ("6 feet", "6 feet"),
    ("6 ft", "6 feet"),
    ("1 lb", "1 pound"),
    ("1 oz", "1 ounce"),
    ("1 hr", "1 hour"),
    ("2 hr", "2 hours"),
    ("2 hrs", "2 hours"),
    ("1 min", "1 minute"),
    ("1°F", "1 degree Fahrenheit"),
    ("2°F", "2 degrees Fahrenheit"),
    ("1°C", "1 degree Celsius"),
    ("1.0 mi", "1.0 miles"),
    ("1.5 mi", "1.5 miles"),
    ("0 mi", "0 miles"),
    ("11 mi", "11 miles"),
    ("21 hrs", "21 hours"),
    ("1 kt", "1 knot"),
    ("1 km/h", "1 kilometer per hour"),
    ("1 m/s", "1 meter per second"),
    ("1 hPa", "1 hectopascal"),
    ("1 inHg", "1 inch of mercury"),
    ("1 mbar", "1 millibar"),
]
assert len(PLURAL_CASES) >= 25

# --- Decimals and negatives ---------------------------------------------------

DECIMAL_CASES = [
    ("-5°F", "-5 degrees Fahrenheit"),
    ("-3.5 in.", "-3.5 inches."),
    ("72.5°F", "72.5 degrees Fahrenheit"),
    ("0.5 in.", "0.5 inches."),
    ("-1°F", "-1 degree Fahrenheit"),
    ("-1 mph", "-1 mile per hour"),
    ("0.5 mi", "0.5 miles"),
    ("-2.5°C", "-2.5 degrees Celsius"),
]
assert len(DECIMAL_CASES) >= 8

# --- Currency: the same count rule -------------------------------------------

CURRENCY_CASES = [
    ("$1", "1 dollar"),
    ("$1.00", "1 dollar"),
    ("$1.01", "1 dollar and 1 cent"),
    ("$2.05", "2 dollars and 5 cents"),
    ("$0.01", "1 cent"),
    ("$45.50", "45 dollars and 50 cents"),
    ("$10", "10 dollars"),
    ("$0.50", "50 cents"),
    ("$1.5", "1 dollar and 50 cents"),
    ("€1", "1 euro"),
    ("€5", "5 euros"),
    ("£1", "1 pound"),
    ("£5", "5 pounds"),
    ("$$", "moderately priced"),
    ("$1,000", "1,000 dollars"),
    ("$1,000 deposit", "1,000 dollars deposit"),
    ("$12,500.50", "12,500 dollars and 50 cents"),
    ("$1,000,000", "1,000,000 dollars"),
    ("$1,000.01", "1,000 dollars and 1 cent"),
    ("€2,500", "2,500 euros"),
    ("£1,000", "1,000 pounds"),
    ("$1, $2", "1 dollar, 2 dollars"),
    ("$1,0000", "1 dollar,0000"),
    ("1, 2, 3", "1, 2, 3"),
    ("1,250 miles", "1,250 miles"),
    ("1,250 mi", "1,250 miles"),
    ("$5 million", "5 million dollars"),
    ("$1.5 billion", "1.5 billion dollars"),
    ("$1.5B", "1.5 billion dollars"),
    ("$300K", "300 thousand dollars"),
    ("$2M", "2 million dollars"),
    ("$1 million", "1 million dollars"),
    ("€2M", "2 million euros"),
    ("£3.5 million", "3.5 million pounds"),
    ("$2.5T.", "2.5 trillion dollars."),
    ("$5m.", "5 million dollars."),
]
assert len(CURRENCY_CASES) >= 35

# --- Ranges --------------------------------------------------------------------

RANGE_CASES = [
    ("60-70°F", "60 to 70 degrees Fahrenheit"),
    ("68-72°F", "68 to 72 degrees Fahrenheit"),
    ("5-10 mph", "5 to 10 miles per hour"),
    ("up, -1.2%", "up, -1.2 percent"),
    ("5% - 10%", "5 percent - 10 percent"),
    ("-1.2% after a rise, -3%", "-1.2 percent after a rise, -3 percent"),
]

assert len(RANGE_CASES) >= 6

# --- Punctuation survives -------------------------------------------------------

PUNCTUATION_CASES = [
    ("29.92 in.", "29.92 inches."),
    ("Rain 2 in. The game", "Rain 2 inches. The game"),
    ("It is 5 in. long", "It is 5 inches long"),
    ("72°.", "72 degrees."),
    ("45%.", "45 percent."),
    ("$45.50.", "45 dollars and 50 cents."),
    ("25mph!", "25 miles per hour!"),
    ("25 mph!", "25 miles per hour!"),
    ("12 mph,", "12 miles per hour,"),
    ("(12 mph)", "(12 miles per hour)"),
    ("5 ft 9 in.", "5 feet 9 inches."),
    ("Is it 12 mph?", "Is it 12 miles per hour?"),
]
assert len(PUNCTUATION_CASES) >= 12

# --- URLs and emails --------------------------------------------------------------

URL_CASES = [
    ("See https://example.com/a?b=1 for 25mph winds", "See for 25 miles per hour winds"),
    ("Email jo.smith@example.com today", "Email jo.smith@example.com today"),
    ("Visit www.example.com now", "Visit now"),
    ("[the forecast](https://example.com/f) says 72°F", "the forecast says 72 degrees Fahrenheit"),
]
assert len(URL_CASES) >= 4

# --- Time, pinned as-is --------------------------------------------------------------

TIME_CASES = [
    ("3:30pm", "3 30 in the afternoon"),
    ("12am", "12 at night"),
    ("12pm", "12 in the afternoon"),
    ("3:05pm", "3 oh 5 in the afternoon"),
    ("3:00 PM", "3 o'clock in the afternoon"),
    ("noon", "noon"),
]
assert len(TIME_CASES) >= 6

# --- Inputs that took a second pass to settle at the base commit -------------------

SETTLE_CASES = [
    ("1 + 2 + 3", "1 plus 2 plus 3"),
    ("a @ + b", "a at plus b"),
    ("$& 5", "budget-friendly and 5"),
    ("6 feet 10 pm", "6 feet 10 in the evening"),
    ("5 ft 9 in the house", "5 feet 9 in the house"),
    ("No $9", "number 9 dollars"),
    ("sq ft 12 in", "Square feet 12 inches"),
    ("sq ft 12 in tall", "Square feet 12 inches tall"),
    ("10-5-1", "10 wins, 5 losses and 1 tie"),
    ("1-0-0", "1 win, 0 losses and 0 ties"),
    ("$$5", "$$5"),
    ("$$ spot", "moderately priced spot"),
    ("#1#2", "number 1 number 2"),
    ("€5€6", "5 euros 6 euros"),
    ("+5+6", "+5 plus 6"),
    ("MD MD", "MD Maryland"),
    ("no0 - 1.0", "number 0 and 1.0"),
    ("12 C$5", "12 degrees Celsius 5 dollars"),
    ("in.&", "in. and"),
]
assert len(SETTLE_CASES) >= 18

GROUPS = {
    "units": UNIT_CASES,
    "gaps": GAP_CASES,
    "false_positives": FALSE_POSITIVE_CASES,
    "plurals": PLURAL_CASES,
    "decimals": DECIMAL_CASES,
    "currency": CURRENCY_CASES,
    "ranges": RANGE_CASES,
    "punctuation": PUNCTUATION_CASES,
    "urls": URL_CASES,
    "time": TIME_CASES,
    "settle": SETTLE_CASES,
}

# Every input and every expected output, deduplicated, in a stable order.
CORPUS = tuple(
    dict.fromkeys(
        text
        for cases in GROUPS.values()
        for pair in cases
        for text in pair
    )
)


def _ids(cases):
    return [repr(src) for src, _ in cases]


@pytest.mark.parametrize("src,expected", UNIT_CASES, ids=_ids(UNIT_CASES))
def test_units_spaced_and_unspaced(src, expected):
    assert n(src) == expected


def test_units_named_member():
    assert n("Winds 25mph.") == "Winds 25 miles per hour."


def test_units_cover_every_token_both_ways():
    for token, _, _ in UNIT_TOKENS:
        for gap in ("", " "):
            assert any(src == f"{count}{gap}{token}" for count in ("1", "5") for src, _ in UNIT_CASES), (token, gap)


@pytest.mark.parametrize("src,expected", GAP_CASES, ids=_ids(GAP_CASES))
def test_measured_gaps_are_fixed(src, expected):
    assert n(src) == expected


def test_gaps_named_member_contract_string():
    src = "Winds 25mph, 15 km/h, 10 m/s, 1013 hPa, 29.92 inHg, it is 72°, 1 mi away, 5 ft 9 in."
    assert n(src) == (
        "Winds 25 miles per hour, 15 kilometers per hour, 10 meters per second, "
        "1013 hectopascals, 29.92 inches of mercury, it is 72 degrees, 1 mile away, 5 feet 9 inches."
    )


@pytest.mark.parametrize("src,expected", FALSE_POSITIVE_CASES, ids=_ids(FALSE_POSITIVE_CASES))
def test_false_positives_are_left_alone(src, expected):
    assert n(src) == expected


def test_false_positives_named_member():
    assert n("Room 5 in Building B") == "Room 5 in Building B"


@pytest.mark.parametrize("src,expected", PLURAL_CASES, ids=_ids(PLURAL_CASES))
def test_unit_agrees_with_count(src, expected):
    assert n(src) == expected


def test_plurals_named_member():
    assert (n("1 mile"), n("2 miles"), n("1 mph"), n("1°F"), n("1.0 mi")) == (
        "1 mile", "2 miles", "1 mile per hour", "1 degree Fahrenheit", "1.0 miles",
    )


@pytest.mark.parametrize("src,expected", DECIMAL_CASES, ids=_ids(DECIMAL_CASES))
def test_decimals_and_negatives(src, expected):
    assert n(src) == expected


def test_decimals_named_member():
    assert n("-1°F") == "-1 degree Fahrenheit"


@pytest.mark.parametrize("src,expected", CURRENCY_CASES, ids=_ids(CURRENCY_CASES))
def test_currency_agrees_with_count(src, expected):
    assert n(src) == expected


def test_currency_named_member():
    assert (n("$1"), n("$1.01"), n("$2.05"), n("$0.01")) == (
        "1 dollar", "1 dollar and 1 cent", "2 dollars and 5 cents", "1 cent",
    )


@pytest.mark.parametrize("src,expected", RANGE_CASES, ids=_ids(RANGE_CASES))
def test_ranges(src, expected):
    assert n(src) == expected


@pytest.mark.parametrize("src,expected", PUNCTUATION_CASES, ids=_ids(PUNCTUATION_CASES))
def test_punctuation_is_preserved(src, expected):
    assert n(src) == expected


def test_punctuation_named_member_sentence_period_kept():
    assert n("Rain 2 in. The game") == "Rain 2 inches. The game"


@pytest.mark.parametrize("src,expected", URL_CASES, ids=_ids(URL_CASES))
def test_urls_and_emails(src, expected):
    assert n(src) == expected


def test_urls_named_member_unit_next_to_url_still_expands():
    assert n("See https://example.com/a?b=1 for 25mph winds") == "See for 25 miles per hour winds"


def test_time_minutes_are_spoken_as_numbers_by_design_3_30pm():
    assert n("3:30pm") == "3 30 in the afternoon"


@pytest.mark.parametrize("src,expected", TIME_CASES, ids=_ids(TIME_CASES))
def test_time_current_style_is_pinned(src, expected):
    assert n(src) == expected


@pytest.mark.parametrize("src,expected", SETTLE_CASES, ids=_ids(SETTLE_CASES))
def test_inputs_that_needed_two_passes_settle_in_one(src, expected):
    assert n(src) == expected


def test_empty_and_none_pass_through():
    assert n("") == ""
    assert n(None) is None


def test_group_floors_and_named_members_exist():
    floors = {
        "units": 120, "gaps": 15, "false_positives": 22, "plurals": 25, "decimals": 8,
        "currency": 35, "ranges": 6, "punctuation": 12, "urls": 4, "time": 6, "settle": 18,
    }
    for name, floor in floors.items():
        assert len(GROUPS[name]) >= floor, name
    assert ("Winds 25mph.", "Winds 25 miles per hour.") in UNIT_CASES
    assert (
        "See https://example.com/a?b=1 for 25mph winds",
        "See for 25 miles per hour winds",
    ) in URL_CASES
    assert ("-1°F", "-1 degree Fahrenheit") in DECIMAL_CASES
    assert ("$0.01", "1 cent") in CURRENCY_CASES


# --- Idempotence ----------------------------------------------------------------------


def test_idempotent_over_every_corpus_input_and_output():
    assert len(CORPUS) >= 300
    failures = [(t, n(t), n(n(t))) for t in CORPUS if n(n(t)) != n(t)]
    assert not failures, failures[:5]


# --- Seeded fuzz (stdlib; D10) ------------------------------------------------------------

_FUZZ_WORDS = [
    "mph", "km/h", "kmh", "kph", "m/s", "kt", "kts", "hPa", "inHg", "in Hg", "mbar", "mb",
    "lb", "lbs", "oz", "kg", "g", "ml", "L", "gal", "qt", "km", "cm", "mm", "mi", "ft", "sq ft",
    "hr", "hrs", "min", "mins", "in", "in.", "F", "C", "am", "pm", "AM", "PM", "m", "N", "S",
    "St", "Dr", "Main", "Room", "Rain", "the", "a", "of", "to", "No", "no", "vs", "CO", "MD", "million", "K", "M", "B",
    "miles per hour", "inches", "degrees", "feet", "kilometers per hour", "dollars",
    "degrees Fahrenheit", "meters per second", "percent",
]
_FUZZ_SYMBOLS = list("°$%:/-.,!?()&#+@€£") + ["°F", "°C"]
_FUZZ_DIGITS = ["10-5-1", "$5", "$1.5", "€5", "£3", "1,000", "12,500", "$1,000", "$1,000.50", "$1,2", "0", "1", "2", "5", "9", "10", "12", "25", "72", "100", "0.5", "1.0", "29.92", "1013", "-1", "-5"]
_FUZZ_URLS = ["https://example.com/a?b=1", "http://a.example/x", "www.example.com/p"]
_FUZZ_EMAILS = ["jo.smith@example.com", "a@b.example"]
_FUZZ_EMOJI = ["😀", "☀"]


def _fuzz_input(rng):
    parts = []
    for _ in range(rng.randint(1, 14)):
        roll = rng.random()
        if roll < 0.35:
            parts.append(rng.choice(_FUZZ_DIGITS))
        elif roll < 0.62:
            parts.append(rng.choice(_FUZZ_WORDS))
        elif roll < 0.84:
            parts.append(rng.choice(_FUZZ_SYMBOLS))
        elif roll < 0.9:
            parts.append(" " + rng.choice(_FUZZ_URLS) + " ")
        elif roll < 0.95:
            parts.append(" " + rng.choice(_FUZZ_EMAILS) + " ")
        else:
            parts.append(rng.choice(_FUZZ_EMOJI))
        parts.append(rng.choice(["", " ", " ", "  "]))
    return "".join(parts)


def _has_content(text):
    """True if some letter/digit survives removing URLs, emoji and whitespace."""
    stripped = text
    for url in _FUZZ_URLS:
        stripped = stripped.replace(url, " ")
    for emoji in _FUZZ_EMOJI:
        stripped = stripped.replace(emoji, " ")
    return any(ch.isalnum() for ch in stripped)


@pytest.mark.parametrize("seed", [1729, 4104])
def test_seeded_fuzz_properties(seed):
    rng = random.Random(seed)
    for _ in range(3000):
        x = _fuzz_input(rng)
        y = n(x)
        assert isinstance(y, str), repr(x)
        assert n(y) == y, f"not idempotent: {x!r} -> {y!r} -> {n(y)!r}"
        assert len(y) <= 8 * len(x) + 64, f"growth: {x!r} -> {y!r}"
        if _has_content(x):
            assert y, f"content lost: {x!r}"


# --- Performance --------------------------------------------------------------------------

_REALISTIC_PARAGRAPH = (
    "Right now it's 72°F in Denver, CO with winds out of the NW at 15 mph, gusting to 25mph. "
    "Humidity is 45% and the barometer reads 29.92 inHg (1013 hPa). Rain 0.5 in. is expected "
    "after 3:30 PM, with a high near 78°F and a low of 54°F. The Broncos play at 7 PM at "
    "123 N Main St, and the game is 28-14 in the fourth quarter. Dinner is about $45.50 for "
    "2 people, 1 mi away, a 12 min walk. See https://example.com/forecast?id=1 for more. "
)


def _realistic(chars):
    return (_REALISTIC_PARAGRAPH * (chars // len(_REALISTIC_PARAGRAPH) + 1))[:chars]


def _best_of(fn, runs=3):
    best = float("inf")
    for _ in range(runs):
        started = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - started)
    return best


def test_realistic_5kb_answer_is_fast():
    text = _realistic(5000)
    assert _best_of(lambda: n(text)) < 0.05


def test_realistic_100kb_answer_is_under_a_second():
    text = _realistic(100_000)
    assert _best_of(lambda: n(text), runs=2) < 1.0


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param("1" * 5000, id="digits"),
        pytest.param("1" + " " * 5000 + "x", id="digit-then-spaces"),
        pytest.param("[" * 5000, id="open-brackets"),
        pytest.param("[a" * 2500, id="bracket-a"),
        pytest.param("(" * 5000, id="open-parens"),
        pytest.param("1." * 2500, id="digit-dot"),
        pytest.param("1 " * 2500, id="digit-space"),
        pytest.param("9" * 2500 + "°" * 2500, id="digits-degrees"),
        pytest.param("100 N" * 1000, id="direction-repeat"),
        pytest.param("$" * 5000, id="dollars"),
        pytest.param("1" * 20000 + " N!", id="digits-then-direction"),
        pytest.param("1" * 20000 + "°C mi in Hg", id="digits-then-unit-letters"),
    ],
)
def test_hostile_input_is_linear(hostile):
    started = time.perf_counter()
    n(hostile)
    assert time.perf_counter() - started < 0.5


def test_digit_runs_longer_than_the_bound_are_not_corrupted():
    long_run = "1" * 12
    assert n(f"{long_run} apples") == f"{long_run} apples"
    assert n(f"{long_run} mph") == f"{long_run} miles per hour"
