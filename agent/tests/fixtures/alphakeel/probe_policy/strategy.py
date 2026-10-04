"""Fixture policy that proves what the sandbox hides: it fails if a credential or the real home is visible."""
import os


def initialize(parameters):
    leaked = [k for k in os.environ if "TOKEN" in k.upper() or "ALPHAKEEL_RESEARCH" in k.upper() or "API_KEY" in k.upper() or "SECRET" in k.upper()]
    if leaked:
        raise RuntimeError(f"credential-like variables visible: {leaked}")
    home = os.environ.get("HOME", "")
    if os.path.exists(os.path.join(home, ".vibe-trading")) or os.path.exists(os.path.join(home, ".ssh")):
        raise RuntimeError("the real user directory is visible")
    return {"home_is_temp": "ak-policy-home" in home}


def on_step(ctx, state):
    return {"intents": [], "state": state}
