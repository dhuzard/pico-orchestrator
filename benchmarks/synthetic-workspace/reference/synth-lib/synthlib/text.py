"""Text helpers of synth-lib."""
import re


def slugify(text):
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
