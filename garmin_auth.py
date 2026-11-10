"""Garmin login shared by every sync.

Reads the same secrets as the original sleep sync:
    GARMIN_EMAIL + GARMIN_PASSWORD    used when the account has no MFA
    GARMIN_TOKEN_B64                  saved session from garmin_login.py
"""

import base64
import io
import os
import tarfile
import time

from garminconnect import Garmin

TOKEN_DIR = os.path.expanduser("~/.garminconnect")
_SECRET_WORDS = ("token", "password", "authorization", "secret", "bearer")


def safe_error(exc):
    """Short error text that cannot echo a credential."""
    text = " ".join(str(exc).split())
    if any(word in text.lower() for word in _SECRET_WORDS):
        return type(exc).__name__
    if len(text) > 180:
        text = text[:180] + "..."
    return text or type(exc).__name__


def is_auth_error(exc):
    return type(exc).__name__ == "GarminConnectAuthenticationError"


def is_rate_limit(exc):
    return type(exc).__name__ == "GarminConnectTooManyRequestsError" or "429" in str(exc)


def garmin_call(fn, *args, **kwargs):
    """Call a Garmin method, waiting out rate limits. Login failures propagate."""
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if is_auth_error(exc) or not is_rate_limit(exc) or attempt == 5:
                raise
            time.sleep(min(60, 10 * (2**attempt)))
    raise RuntimeError("Garmin retry loop ended")


def restore_garmin_token():
    """Unpack the base64 token secret into the local token directory."""
    token_b64 = os.environ["GARMIN_TOKEN_B64"]
    raw = base64.b64decode(token_b64)
    os.makedirs(TOKEN_DIR, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(TOKEN_DIR)


def garmin_client():
    # Simple path: email + password set as secrets (works if your Garmin
    # account has no multi-factor authentication).
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if email and password:
        garmin = Garmin(email=email, password=password)
        garmin.login()
        return garmin

    # Secure path: a token created once with garmin_login.py (needed if you use MFA).
    if os.environ.get("GARMIN_TOKEN_B64"):
        restore_garmin_token()
        garmin = Garmin()
        garmin.login(TOKEN_DIR)  # resumes the saved session, no password needed
        return garmin

    raise SystemExit(
        "No Garmin credentials found. Set either GARMIN_EMAIL + GARMIN_PASSWORD, "
        "or GARMIN_TOKEN_B64."
    )
