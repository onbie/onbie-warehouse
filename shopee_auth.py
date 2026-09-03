"""
shopee_auth.py
==============
Shopee OpenAPI v2 OAuth helper for Onbie Packing System.

Handles:
- HMAC-SHA256 signature generation (per Shopee v2 spec)
- Access token exchange via /api/v2/auth/token/get
- Token persistence: Supabase (durable, survives Streamlit Cloud restarts
  and refresh_token rotation) with tokens.json as a local-development /
  Supabase-unavailable fallback.

This module has NO hard dependency on Streamlit — it reads configuration
from os.environ, not st.secrets, so it can still be tested and reused
independently of app.py (app.py is what bridges st.secrets → os.environ).

Environment variables required (set on Streamlit Community Cloud):
    SHOPEE_PARTNER_ID    integer partner ID from Shopee Open Platform
    SHOPEE_PARTNER_KEY   secret key string from Shopee Open Platform

Environment variables for persistent Supabase token storage (optional —
everything falls back to tokens.json if these are unset or Supabase is
unreachable):
    SUPABASE_URL         Supabase project URL
    SUPABASE_KEY         Supabase service role or anon key with access to
                          the shopee_tokens table

Expected Supabase table (create once, e.g. via the SQL editor):
    create table shopee_tokens (
        shop_id       bigint primary key,
        access_token  text,
        refresh_token text,
        expire_in     bigint,
        fetch_time    bigint,
        partner_id    bigint
    );
"""

# =============================================================================
# tokens.json is now a FALLBACK only — used for local development, or as a
# safety net if Supabase is unreachable/unconfigured. The source of truth
# for persistence across Streamlit Cloud restarts (and across refresh_token
# rotation, which Shopee does on every refresh) is Supabase: it's the only
# one of the three options that the running app can actually write to at
# runtime — tokens.json is wiped on every container restart, and Streamlit
# Secrets are read-only from inside the app.
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
TOKEN_PATH   = "/api/v2/auth/token/get"
REFRESH_PATH = "/api/v2/auth/access_token/get"
AUTH_PATH    = "/api/v2/shop/auth_partner"
TOKENS_FILE  = "tokens.json"

# Timeout for all outbound requests to Shopee API (seconds).
REQUEST_TIMEOUT_SECONDS = 15

# Refresh the access token this many seconds before it actually expires,
# to avoid race conditions and clock skew.
TOKEN_EXPIRY_BUFFER_SECONDS = 300  # 5 minutes


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
# Supabase persistent token storage
# ---------------------------------------------------------------------------
# This is the durable, cross-restart, cross-rotation source of truth. Unlike
# tokens.json (wiped on every Streamlit Cloud container restart) or
# Streamlit secrets (read-only from inside the running app — there is no
# API to write an updated refresh_token back into them), Supabase is an
# external store the app can both read AND write at runtime. Every token
# save (OAuth connect, or a refresh picking up a rotated refresh_token)
# writes here immediately, so the next restart always picks up the current
# token, not a stale one.
#
# Every function below is best-effort and never raises out of this module's
# public API: if SUPABASE_URL/SUPABASE_KEY aren't set, the `supabase`
# package isn't installed, or a network/DB error occurs, callers fall back
# to tokens.json transparently (see load_tokens() / _persist_tokens()).

SUPABASE_TABLE = "shopee_tokens"

_supabase_client = None
_supabase_client_init_attempted = False


def _get_supabase_client():
    """Lazily create and cache a Supabase client from SUPABASE_URL /
    SUPABASE_KEY in os.environ. Returns None (never raises) if not
    configured, the `supabase` package isn't installed, or the client
    can't be created — every caller must handle a None return."""
    global _supabase_client, _supabase_client_init_attempted
    if _supabase_client is not None:
        return _supabase_client
    if _supabase_client_init_attempted:
        return None
    _supabase_client_init_attempted = True

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        logger.debug("Supabase not configured (SUPABASE_URL/SUPABASE_KEY unset) — using tokens.json only.")
        return None

    try:
        from supabase import create_client
        _supabase_client = create_client(url, key)
        return _supabase_client
    except Exception as e:
        logger.warning("Supabase client init failed, falling back to tokens.json: %s", e)
        return None


def _supabase_load_tokens() -> Optional[Dict]:
    """Fetch the most recently updated token row from Supabase. Returns
    None if Supabase isn't configured/reachable or no row exists yet."""
    client = _get_supabase_client()
    if client is None:
        return None
    try:
        result = (
            client.table(SUPABASE_TABLE)
            .select("shop_id, access_token, refresh_token, expire_in, fetch_time, partner_id")
            .order("fetch_time", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return None
        return dict(rows[0])
    except Exception as e:
        logger.warning("Supabase token load failed, falling back to tokens.json: %s", e)
        return None


def _supabase_save_tokens(tokens: Dict) -> None:
    """Upsert the current tokens into Supabase, keyed by shop_id. Raises on
    failure so the caller (_persist_tokens) can log it — but a failure here
    never prevents the local tokens.json write from succeeding."""
    client = _get_supabase_client()
    if client is None:
        return
    row = {
        "shop_id":       tokens.get("shop_id"),
        "access_token":  tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expire_in":     tokens.get("expire_in"),
        "fetch_time":    tokens.get("fetch_time"),
        "partner_id":    tokens.get("partner_id"),
    }
    client.table(SUPABASE_TABLE).upsert(row, on_conflict="shop_id").execute()


def _persist_tokens(tokens: Dict) -> Dict:
    """Persist tokens to both stores: tokens.json (always — local dev
    fallback / safety net) and Supabase (best-effort — the durable,
    cross-restart source of truth). A Supabase failure is logged but never
    raised, so OAuth connect / token refresh still succeeds locally even if
    Supabase is temporarily unreachable."""
    _write_tokens_file(tokens)
    try:
        _supabase_save_tokens(tokens)
    except Exception as e:
        logger.warning("Supabase token save failed (tokens.json still updated): %s", e)
    return tokens


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def _write_tokens_file(tokens: Dict) -> Dict:
    """Write a tokens dict to TOKENS_FILE as-is. Local-development /
    Supabase-unavailable fallback — see _persist_tokens(). Also used
    directly by bootstrap_tokens_from_secrets()'s legacy path.
    """
    try:
        with open(TOKENS_FILE, "w") as f:
            json.dump(tokens, f, indent=2)
    except OSError as e:
        raise RuntimeError(
            f"Token save failed: could not write to {TOKENS_FILE}. Detail: {e}"
        )
    return tokens


def save_tokens(data: Dict, shop_id: int) -> Dict:
    """Extract token fields from Shopee response and persist them (Supabase
    first, tokens.json always as fallback — see _persist_tokens()).

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

    if os.path.exists(TOKENS_FILE):
        logger.warning(
            "tokens.json already exists — existing token for shop_id=%s "
            "will be overwritten with new token.", shop_id
        )
    _persist_tokens(tokens)

    logger.info(
        "Tokens saved (expire_in=%ss, fetch_time=%s)",
        expire_in, fetch_time,
    )

    return tokens


def bootstrap_tokens_from_secrets(
    shop_id,
    refresh_token: str,
    partner_id: Optional[int] = None,
    access_token: str = "",
    expire_in: int = 0,
    fetch_time: int = 0,
) -> Dict:
    """Seed tokens.json from a persistent, non-ephemeral source (e.g.
    Streamlit Cloud secrets) so a fresh container doesn't require the user
    to click Connect Shopee again after every restart/redeploy.

    Only shop_id and refresh_token are required. access_token/expire_in/
    fetch_time default to values that make is_token_expired() immediately
    return True, so the very next get_valid_access_token() call refreshes
    and obtains a real access_token — exactly the same path an already
    -expired token takes, no special-casing required elsewhere.

    This function always overwrites tokens.json — callers are responsible
    for only invoking it when no local tokens.json already exists (see
    load_tokens() is None), so a live, possibly-rotated refresh_token
    already on disk from earlier in this container's lifetime is never
    clobbered by a stale value from secrets.

    Args:
        shop_id:       integer shop ID.
        refresh_token: Shopee refresh token, sourced from persistent storage.
        partner_id:    integer partner ID. If omitted, read from
                       get_credentials() (i.e. os.environ, same as everywhere
                       else in this module).
        access_token, expire_in, fetch_time: optional; defaults force an
                       immediate refresh on next use.

    Returns:
        The dict that was written to disk.

    Raises:
        ValueError:   shop_id or refresh_token missing.
        RuntimeError: credentials missing, or tokens.json could not be written.
    """
    if not shop_id:
        raise ValueError("bootstrap_tokens_from_secrets: shop_id is required.")
    if not refresh_token:
        raise ValueError("bootstrap_tokens_from_secrets: refresh_token is required.")

    if partner_id is None:
        partner_id, _ = get_credentials()

    tokens = {
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "expire_in":     expire_in,
        "fetch_time":    fetch_time,
        "shop_id":       int(shop_id),
        "partner_id":    int(partner_id),
    }

    logger.info(
        "Bootstrapping tokens.json from persistent secrets for shop_id=%s "
        "(no local tokens.json was found).", shop_id,
    )
    return _write_tokens_file(tokens)


def load_tokens() -> Optional[Dict]:
    """Load saved tokens. Supabase (if configured and reachable) is checked
    first — it's the durable, cross-restart, cross-rotation source of
    truth. Falls back to the local tokens.json file for local development
    or if Supabase is unreachable/unconfigured.

    Returns:
        Token dict if found in Supabase or tokens.json, otherwise None.
    """
    supabase_tokens = _supabase_load_tokens()
    if supabase_tokens is not None:
        return supabase_tokens

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
# Token expiry check
# ---------------------------------------------------------------------------

def is_token_expired(tokens: Dict) -> bool:
    """Return True if the access token is expired or within the expiry buffer.

    Uses fetch_time + expire_in to compute the absolute expiry timestamp,
    then compares it against now + TOKEN_EXPIRY_BUFFER_SECONDS so the
    token is refreshed slightly before it actually expires.

    Args:
        tokens: token dict as returned by load_tokens() or save_tokens().

    Returns:
        True  — token is expired or will expire within TOKEN_EXPIRY_BUFFER_SECONDS.
        False — token is still valid with sufficient margin.
    """
    fetch_time = tokens.get("fetch_time", 0)
    expire_in  = tokens.get("expire_in", 0)

    if not fetch_time or not expire_in:
        # Missing expiry info — treat as expired to force a refresh.
        logger.warning("Token expiry info missing; treating token as expired.")
        return True

    expiry_ts = fetch_time + expire_in
    now       = int(time.time())
    remaining = expiry_ts - now

    logger.debug(
        "Token expiry check: fetch_time=%s expire_in=%s expiry_ts=%s now=%s remaining=%ss",
        fetch_time, expire_in, expiry_ts, now, remaining,
    )

    return remaining <= TOKEN_EXPIRY_BUFFER_SECONDS


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def refresh_access_token(tokens: Dict) -> Dict:
    """Refresh an expired (or near-expired) Shopee access token.

    Calls POST /api/v2/auth/access_token/get with the stored refresh_token,
    shop_id, and partner_id, using the Shopee v2 public signature variant
    (same base string as the initial token exchange):
        {partner_id}{api_path}{timestamp}

    Args:
        tokens: current token dict as returned by load_tokens(). Must contain
                refresh_token, shop_id, and partner_id.

    Returns:
        Raw JSON response dict from Shopee (unfiltered). Pass this to
        save_tokens() to persist the new token.

    Raises:
        ValueError:            missing required fields in tokens dict, or
                               invalid/missing credentials in os.environ.
        requests.Timeout:      network request timed out.
        requests.HTTPError:    HTTP 4xx or 5xx from Shopee.
        json.JSONDecodeError:  response body is not valid JSON.
        RuntimeError:          Shopee returned HTTP 200 with an error in body.
    """
    refresh_token = tokens.get("refresh_token", "")
    shop_id       = tokens.get("shop_id", 0)
    # partner_id stored in tokens is used as a sanity source; credentials
    # are still re-read from os.environ to pick up any rotation.
    stored_partner_id = tokens.get("partner_id", 0)

    if not refresh_token:
        raise ValueError(
            "Token refresh failed: 'refresh_token' is missing from saved tokens. "
            "Re-authorize via Connect Shopee."
        )
    if not shop_id:
        raise ValueError(
            "Token refresh failed: 'shop_id' is missing from saved tokens. "
            "Re-authorize via Connect Shopee."
        )

    partner_id, partner_key = get_credentials()

    if stored_partner_id and int(stored_partner_id) != partner_id:
        logger.warning(
            "partner_id mismatch: tokens.json has %s but env has %s. "
            "Using env value.", stored_partner_id, partner_id,
        )

    timestamp = int(time.time())
    sign = generate_signature(partner_id, REFRESH_PATH, timestamp, partner_key)

    url = (
        f"{SHOPEE_HOST}{REFRESH_PATH}"
        f"?partner_id={partner_id}&timestamp={timestamp}&sign={sign}"
    )

    body = {
        "refresh_token": refresh_token,
        "shop_id":       int(shop_id),
        "partner_id":    partner_id,
    }

    logger.info("Refreshing access token for shop_id=%s ...", shop_id)
    logger.info("Endpoint: POST %s%s", SHOPEE_HOST, REFRESH_PATH)
    # refresh_token is NOT logged — treat it as a secret.

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
            f"{REQUEST_TIMEOUT_SECONDS} seconds during token refresh."
        )
    except requests.ConnectionError as e:
        raise requests.ConnectionError(
            f"Network error during token refresh: {e}"
        )

    logger.info("Shopee refresh response status: %s", response.status_code)

    if response.status_code == 401:
        raise requests.HTTPError(
            "HTTP 401 during token refresh: signature or partner_id is invalid."
        )
    if response.status_code == 403:
        raise requests.HTTPError(
            "HTTP 403 during token refresh: refresh_token may be expired or revoked. "
            "Re-authorize via Connect Shopee."
        )
    if response.status_code == 500:
        raise requests.HTTPError(
            "HTTP 500 from Shopee during token refresh. Try again later."
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise requests.HTTPError(
            f"HTTP error during token refresh (status {response.status_code}): {e}"
        )

    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        raise json.JSONDecodeError(
            f"JSON parse failure during token refresh. "
            f"Raw response (first 200 chars): {response.text[:200]!r}",
            doc=response.text,
            pos=0,
        )

    error_code = data.get("error", "")
    if error_code:
        error_msg = data.get("message", "no message")
        known_errors = {
            "error_auth":         "refresh_token is invalid or expired. Re-authorize via Connect Shopee.",
            "error_param":        "Invalid request parameters during refresh.",
            "error_permission":   "Permission denied during token refresh.",
            "error_server":       "Shopee server error during refresh. Try again later.",
            "error_sign_invalid": "Invalid signature during refresh. Check SHOPEE_PARTNER_KEY.",
        }
        friendly = known_errors.get(error_code, "")
        detail = f" ({friendly})" if friendly else ""
        raise RuntimeError(
            f"Shopee token refresh error [{error_code}]: {error_msg}{detail}"
        )

    logger.info("Token refresh successful for shop_id=%s.", shop_id)
    return data


# ---------------------------------------------------------------------------
# High-level token accessor
# ---------------------------------------------------------------------------

def get_valid_access_token() -> str:
    """Return a valid Shopee access token, refreshing automatically if needed.

    Load-check-refresh cycle:
    1. Load tokens from tokens.json.
    2. If missing → raise RuntimeError (re-auth required).
    3. If expired or within TOKEN_EXPIRY_BUFFER_SECONDS of expiry → refresh.
    4. Save refreshed tokens back to tokens.json.
    5. Return the access_token string.

    Returns:
        access_token string, guaranteed to be valid for at least
        TOKEN_EXPIRY_BUFFER_SECONDS more seconds (barring Shopee-side
        revocation).

    Raises:
        RuntimeError: tokens.json not found, or refresh failed and the
                      caller should prompt re-authorization.
        ValueError:   credentials missing from os.environ.
        requests.*:   network errors during refresh (propagated from
                      refresh_access_token()).
    """
    tokens = load_tokens()

    if tokens is None:
        raise RuntimeError(
            "No saved tokens found. Authorize Shopee via Connect Shopee first."
        )

    if is_token_expired(tokens):
        logger.info("Access token expired or near expiry — refreshing...")
        data = refresh_access_token(tokens)
        shop_id = tokens.get("shop_id", 0)
        tokens  = save_tokens(data, shop_id)
        logger.info("Token refreshed and saved.")
    else:
        logger.debug("Access token is still valid — no refresh needed.")

    access_token = tokens.get("access_token", "")
    if not access_token:
        raise RuntimeError(
            "access_token is empty after load/refresh cycle. "
            "Re-authorize via Connect Shopee."
        )

    return access_token


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