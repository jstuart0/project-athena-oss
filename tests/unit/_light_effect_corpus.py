"""The light-effect utterance corpus, shared by the fast-path parity test
(tests/unit/test_light_effect_fast_path.py) and the classify-route test
(tests/unit/test_light_effect_classify.py), so both routes are held to the same
expected answers.

Fictional rooms and lights only. The expectations assume the default test room
directory (study, bedroom, guest room, living room) and the contract fixtures'
effect lists (Govee-style and WLED-style) with a light called "Study Prism".
"""
from __future__ import annotations

# The utterances that must classify CONTROL through the effect route.
POSITIVES = [
    "set the study prism to candlelight",
    "set the study to candlelight",
    "make the study twinkle",
    "turn off the effect",
    "turn off the candlelight effect",
    "turn the effect off",
    "candlelight mode off",
    "candlelight mode on the study prism",
    "stop the twinkle on the study prism",
    "play the candle effect on the study prism",
    "put the study on fade",
    "set the study to fire 2012",
    "turn off the effects in the study",
    "turn off the effects on the guest room",
    "no more effects",
    "sunset effect on the study prism",
    "can you turn off the effect",
    "set bedroom 2 to candlelight",
]

# The F67 T4 negatives (nothing here is an effect request, with the full
# vocabulary loaded) and the other commands the effect grammar must not take.
T4_NEGATIVES = [
    "turn off guest mode",
    "turn off away mode",
    "turn off sleep mode",
    "game mode off",
    "turn off the sound effects",
    "stop the stream",
    "stop the rain",
    "stop the washing machine",
    "stop the noise",
]
OTHER_NEGATIVES = [
    "turn on the sound effect",
    "play the rain sound effect in the living room",
    "romantic mode",
    "night mode on the study prism",
    "set the study lights to 50 percent",
    "turn on the kitchen light",
    "fade the lights",
    "set the study to blue",
    "turn off the effects on the tv",
    "turn off the effects on the guest",
    "turn off the effects for the neighbours",
    "is the candlelight effect on the study prism",
    "what effect is on the study prism",
]
NEGATIVES = T4_NEGATIVES + OTHER_NEGATIVES

PARITY_CORPUS = [(q, True) for q in POSITIVES] + [(q, False) for q in NEGATIVES]
