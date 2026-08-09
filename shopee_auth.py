"""
shopee_auth.py
==============
Shopee OpenAPI v2 OAuth helper for Onbie Packing System.

Handles:
- HMAC-SHA256 signature generation (per Shopee v2 spec)
- Access token exchange via /api/v2/auth/token/get
- Token persistence to tokens.json

This module has NO dependency on Streamlit and NO knowledge of the
packing system. It is intentionally kept as a plain Python module so
it can be tested and reused independently of app.py.

Environment variables required (set on Streamlit Community Cloud):
    SHOPEE_PARTNER_ID    integer partner ID from Shopee Open Platform
    SHOPEE_PARTNER_KEY   secret key string from Shopee Open Platform
"""

# =============================================================================
# DEVELOPMENT ONLY
# tokens.json digunakan sementara untuk membuktikan OAuth flow berjalan.
# Production sebaiknya menggunakan database atau secure secret storage
# (misalnya: Supabase, PostgreSQL, AWS Secrets Manager, atau Streamlit
# Secrets yang di-read ke memory saja tanpa ditulis ke disk).
# tokens.json TIDAK persisten di Streamlit Community Cloud — file ini
# akan hilang setiap kali app di-reboot atau re-deploy.
# =============================================================================

import os
import json
import hmac
import hashlib
import time
import logging
import requests
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SHOPEE_HOST = "https://partner.shopeemobile.com"
TOKEN_PATH  = "/api/v2/auth/token/get"
AUTH_PATH   = "/api/v2/shop/auth_partner"
TOKENS_FILE = "tokens.json"

# Timeout for all outbound requests to Shopee API (seconds).
REQUEST_TIMEOUT_SECONDS = 15


# ---------------------------------------------------------------------------
# Auth URL
# ---------------------------------------------------------------------------

def generate_auth_url(redirect_url: str) -> str:
    """Build the Shopee authorization URL to redirect the user to.

    The user opens this URL in their browser, logs in to Shopee, and
    approves the authorization. Shopee then redirects back to redirect_url
    with ?code=...&shop_id=... appended.

    Signature base string (Shopee v2 public variant):
        {partner_id}{api_path}{timestamp}

    Args:
        redirect_url: the URL Shopee should redirect back to after authorization.
                      Must match exactly what is registered in Shopee Open Platform.
                      Example: "http://localhost:8501" for local development.

    Returns:
        Full authorization URL string. Open this in the browser to start OAuth.

    Raises:
        ValueError: if credentials are missing or malformed.
    """
    partner_id, partner_key = get_credentials()
    timestamp = int(time.time())
    sign = generate_signature(partner_id, AUTH_PATH, timestamp, partner_key)

    url = (
        f"{SHOPEE_HOST}{AUTH_PATH}"
        f"?partner_id={partner_id}"
        f"&timestamp={timestamp}"
        f"&sign={sign}"
        f"&redirect={redirect_url}"
    )

    logger.info("Auth URL generated (redirect=%s)", redirect_url)
    return url


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def get_credentials() -> Tuple[int, str]:
    """Read partner credentials from environment variables.

    Returns:
        (partner_id: int, partner_key: str)

    Raises:
        ValueError: if either variable is missing or partner_id is not an integer.
    """
    partner_id_str = os.environ.get("SHOPEE_PARTNER_ID", "").strip()
    partner_key    = os.environ.get("SHOPEE_PARTNER_KEY", "").strip()

    if not partner_id_str:
        raise ValueError(
            "Missing credential: environment variable SHOPEE_PARTNER_ID is not set. "
            "Set it in Streamlit Cloud → Settings → Secrets."
        )
    if not partner_key:
        raise ValueError(
            "Missing credential: environment variable SHOPEE_PARTNER_KEY is not set. "
            "Set it in Streamlit Cloud → Settings → Secrets."
        )
    try:
        partner_id = int(partner_id_str)
    except ValueError:
        raise ValueError(
            f"Invalid credential: SHOPEE_PARTNER_ID must be an integer, "
            f"got {partner_id_str!r}."
        )

    return partner_id, partner_key


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

def generate_signature(
    partner_id: int,
    api_path: str,
    timestamp: int,
    partner_key: str,
) -> str:
    """Generate HMAC-SHA256 signature for a Shopee v2 public API call.

    This is the "public" variant (no access_token or shop_id in base string),
    used specifically for /api/v2/auth/token/get.

    Base string format (per Shopee OpenAPI v2 documentation):
        {partner_id}{api_path}{timestamp}

    Args:
        partner_id:  integer partner ID
        api_path:    API path string, e.g. "/api/v2/auth/token/get"
        timestamp:   Unix timestamp (integer seconds)
        partner_key: secret key string — NEVER logged or printed

    Returns:
        Hex-encoded HMAC-SHA256 digest string.
    """
    base_string = f"{partner_id}{api_path}{timestamp}"
    # NOTE: partner_key is intentionally NOT logged anywhere in this function.
    signature = hmac.new(
        partner_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    # Log base string for debugging — safe because it contains no secrets.
    logger.debug("Signature base string: %r", base_string)
    # Signature value is NOT logged — in production it could aid replay attacks.
    return signature


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

def exchange_code_for_token(code: str, shop_id: int) -> Dict:
    """Exchange a Shopee authorization code for access + refresh tokens.

    Calls POST /api/v2/auth/token/get per Shopee OpenAPI v2 spec.

    Args:
        code:    one-time authorization code from Shopee OAuth redirect URL
        shop_id: integer shop ID from Shopee OAuth redirect URL

    Returns:
        Full JSON response dict from Shopee (raw, unfiltered).

    Raises:
        ValueError:            missing/invalid credentials, missing code or shop_id
        requests.Timeout:      network request timed out (REQUEST_TIMEOUT_SECONDS limit)
        requests.HTTPError:    HTTP 4xx or 5xx from Shopee
        json.JSONDecodeError:  response body is not valid JSON
        RuntimeError:          Shopee returned HTTP 200 but with an error in the body
    """
    if not code or not str(code).strip():
        raise ValueError("Missing parameter: authorization code (code) is empty.")
    if not shop_id:
        raise ValueError("Missing parameter: shop_id is empty or zero.")

    partner_id, partner_key = get_credentials()
    timestamp = int(time.time())

    sign = generate_signature(partner_id, TOKEN_PATH, timestamp, partner_key)

    url = (
        f"{SHOPEE_HOST}{TOKEN_PATH}"
        f"?partner_id={partner_id}&timestamp={timestamp}&sign={sign}"
    )

    body = {
        "code":       str(code).strip(),
        "shop_id":    int(shop_id),
        "partner_id": partner_id,
    }

    logger.info("Exchanging authorization code for access token...")
    logger.info("Endpoint: POST %s%s", SHOPEE_HOST, TOKEN_PATH)
    logger.info("Shop ID: %s", shop_id)
    # NOTE: 'code' is a one-time token — log only first 6 chars for traceability.
    logger.info("Code (partial): %s...", str(code)[:6])

    try:
        response = requests.post(
            url,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        raise requests.Timeout(
            f"Network timeout: Shopee API did not respond within "
            f"{REQUEST_TIMEOUT_SECONDS} seconds. "
            "Check your internet connection and try again."
        )
    except requests.ConnectionError as e:
        raise requests.ConnectionError(
            f"Network error: could not reach Shopee API. Detail: {e}"
        )

    logger.info("Shopee HTTP response status: %s", response.status_code)

    # Handle specific HTTP error codes with clear messages before raise_for_status
    if response.status_code == 401:
        raise requests.HTTPError(
            "HTTP 401 Unauthorized: Partner ID or signature is invalid. "
            "Check SHOPEE_PARTNER_ID and SHOPEE_PARTNER_KEY."
        )
    if response.status_code == 403:
        raise requests.HTTPError(
            "HTTP 403 Forbidden: Access denied. Your app may not have permission "
            "for this shop, or the authorization code has already been used."
        )
    if response.status_code == 500:
        raise requests.HTTPError(
            "HTTP 500 Internal Server Error from Shopee. This is a Shopee-side issue. "
            "Try again in a few minutes."
        )

    # For any other 4xx/5xx not caught above
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise requests.HTTPError(
            f"HTTP error from Shopee (status {response.status_code}): {e}"
        )

    # Parse response body
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        raise json.JSONDecodeError(
            f"JSON parse failure: Shopee returned a non-JSON response. "
            f"Raw response (first 200 chars): {response.text[:200]!r}",
            doc=response.text,
            pos=0,
        )

    # Shopee returns HTTP 200 even for application-level errors — check body
    error_code = data.get("error", "")
    if error_code:
        error_msg = data.get("message", "no message")
        # Map known Shopee error codes to human-readable messages
        known_errors = {
            "error_auth":         "Invalid authorization code. The code may have already been used or has expired.",
            "error_param":        "Invalid request parameters. Check code, shop_id, and partner_id.",
            "error_permission":   "Permission denied. Your app may not be authorized for this shop.",
            "error_server":       "Shopee server error. Try again later.",
            "error_not_found":    "Resource not found. Check that shop_id is correct.",
            "error_sign_invalid": "Invalid signature. Check SHOPEE_PARTNER_KEY and signature logic.",
        }
        friendly = known_errors.get(error_code, "")
        detail = f" ({friendly})" if friendly else ""
        raise RuntimeError(
            f"Shopee API error [{error_code}]: {error_msg}{detail}"
        )

    logger.info("Token exchange successful.")
    return data


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def save_tokens(data: Dict, shop_id: int) -> Dict:
    """Extract token fields from Shopee response and persist to tokens.json.

    Saved fields match the format required by subsequent Shopee API calls:
        access_token   — bearer token for API calls
        refresh_token  — used to renew access_token before expiry
        expire_in      — token lifetime in seconds (from Shopee response)
        fetch_time     — Unix timestamp at which the token was fetched
                         (use this + expire_in to compute absolute expiry)
        shop_id        — integer shop ID this token belongs to
        partner_id     — integer partner ID (from env var)

    Args:
        data:    raw JSON dict from exchange_code_for_token()
        shop_id: integer shop ID from OAuth redirect

    Returns:
        The dict that was written to disk (safe to display, tokens masked elsewhere).

    Raises:
        RuntimeError: if required token fields are missing from Shopee response,
                      or if the file cannot be written.
    """
    access_token  = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    expire_in     = data.get("expire_in", 0)

    if not access_token:
        raise RuntimeError(
            "Token save failed: Shopee response did not include 'access_token'. "
            f"Response keys received: {list(data.keys())}"
        )
    if not refresh_token:
        raise RuntimeError(
            "Token save failed: Shopee response did not include 'refresh_token'. "
            f"Response keys received: {list(data.keys())}"
        )

    partner_id, _ = get_credentials()
    fetch_time = int(time.time())

    tokens = {
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "expire_in":     expire_in,
        "fetch_time":    fetch_time,
        "shop_id":       int(shop_id),
        "partner_id":    partner_id,
    }

    try:
        if os.path.exists(TOKENS_FILE):
            logger.warning(
                "tokens.json already exists — existing token for shop_id=%s "
                "will be overwritten with new token.", shop_id
            )
        with open(TOKENS_FILE, "w") as f:
            json.dump(tokens, f, indent=2)
    except OSError as e:
        raise RuntimeError(
            f"Token save failed: could not write to {TOKENS_FILE}. Detail: {e}"
        )

    logger.info(
        "Tokens saved to %s (expire_in=%ss, fetch_time=%s)",
        TOKENS_FILE, expire_in, fetch_time,
    )

    # TODO: Implement refresh token flow before access_token expires.
    # - access_token expires in `expire_in` seconds from `fetch_time`.
    # - Use refresh_token to call /api/v2/auth/access_token/get before expiry.
    # - Refresh token itself expires in 30 days (Shopee default).
    # - Store renewed tokens back to persistent storage (replace tokens.json
    #   with a proper database or Streamlit Secrets before production use).

    return tokens


def load_tokens() -> Optional[Dict]:
    """Load saved tokens from tokens.json.

    Returns:
        Token dict if file exists and is valid JSON, otherwise None.
    """
    if not os.path.exists(TOKENS_FILE):
        logger.debug("tokens.json not found — no saved tokens.")
        return None
    try:
        with open(TOKENS_FILE) as f:
            tokens = json.load(f)
        logger.debug("tokens.json loaded successfully.")
        return tokens
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load tokens.json: %s", e)
        return None


# ---------------------------------------------------------------------------
# Token masking (for safe display)
# ---------------------------------------------------------------------------

def mask_token(token: str) -> str:
    """Return a masked version of a token string safe for display in UI/logs.

    Format: first 6 chars + '...' + last 4 chars.
    If the token is too short to mask meaningfully, return '***'.

    Example:
        'abcdef1234567890xyz1234' -> 'abcdef...1234'
    """
    if not token or len(token) < 12:
        return "***"
    return f"{token[:6]}...{token[-4:]}"


# ---------------------------------------------------------------------------
# High-level entry point (called by app.py)
# ---------------------------------------------------------------------------

def handle_oauth_callback(code: str, shop_id: int) -> Dict:
    """Full OAuth callback flow: validate inputs → exchange code → save tokens.

    This is the single function called by app.py. All errors propagate
    upward so the caller (app.py) can display a clear error message.

    Args:
        code:    authorization code from Shopee redirect URL
        shop_id: shop ID from Shopee redirect URL

    Returns:
        The token dict that was saved to tokens.json.
    """
    logger.info("OAuth callback received.")
    logger.info("Shop ID: %s", shop_id)
    logger.info("Exchanging authorization code...")

    data  = exchange_code_for_token(code, shop_id)
    saved = save_tokens(data, shop_id)

    logger.info("OAuth flow complete. Access token saved.")
    return saved