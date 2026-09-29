"""
tiktok_auth.py
==============
TikTok Shop (Tokopedia, Indonesia) SELLER OAuth helper for Onbie Packing
System. Sibling of shopee_auth.py, kept fully separate from it.

Handles:
- Reading TIKTOK_APP_KEY / TIKTOK_APP_SECRET from os.environ
- Exchanging the one-time authorization `code` (from the /tiktok_callback
  redirect) for access + refresh tokens
- Signing and sending GET /authorization/202309/shops to retrieve the
  authorized shop(s), including the shop_cipher later API calls need
- Saving the result to a local tiktok_tokens.json (interim storage only)

Like shopee_auth.py this module has NO dependency on Streamlit: it reads
configuration from os.environ, not st.secrets. pages/tiktok_callback.py is
what bridges st.secrets -> os.environ.

SCOPE OF THIS FILE (deliberately limited):
    - Seller authorization + token exchange + authorized-shop lookup only.
    - NO order syncing / order API calls.
    - NO token refresh yet.
    - NO Supabase. Tokens go to a local tiktok_tokens.json only, which is
      wiped on every Streamlit Community Cloud container restart. A durable
      tiktok_tokens table is a separate, not-yet-approved step.
    - tiktok_tokens.json holds live credentials in plain text — it must
      never be committed to git.

SECRET HANDLING — hard rule for this module:
    The auth code, app secret, access token, refresh token and shop_cipher
    are NEVER printed, logged, or placed in an exception message.
    - Nothing here logs a URL or a response body (TikTok's token endpoint
      takes app_secret and auth_code as query-string parameters, so a URL
      is itself a secret).
    - requests exceptions embed the full request URL in their text, so
      every one is caught and re-raised as a TikTokAuthError carrying only
      a generic message plus the exception's class name.
    - handle_oauth_callback() returns a summary that contains none of the
      values above, so callers can display it without risk.
    This is stricter than shopee_auth.py, which shows masked token
    fragments. Here there is no masking helper at all.

Environment variables (bridged from Streamlit secrets by the callback page):
    TIKTOK_APP_KEY       app key from TikTok Shop Partner Center
    TIKTOK_APP_SECRET    app secret from TikTok Shop Partner Center
"""

import os
import json
import hmac
import time
import hashlib
import logging
import requests
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Onbie's TikTok Shop service (Partner Center > App & Service). Not a secret.
# The seller authorization URL itself is NOT built here — it is copied
# verbatim from Partner Center's "Copy authorization link" button.
TIKTOK_SERVICE_ID = "7689258529156368134"

# Scopes enabled for this service in Partner Center (Manage API). They are
# configured there, not passed by our code; kept here only so the callback
# can flag a scope that TikTok did not report as granted.
EXPECTED_SCOPES = [
    "seller.order.info",
    "seller.logistics",
    "seller.authorization.info",
]

AUTH_HOST = "https://auth.tiktok-shops.com"
API_HOST = "https://open-api.tiktokglobalshop.com"
TOKEN_GET_PATH = "/api/v2/token/get"
AUTHORIZED_SHOPS_PATH = "/authorization/202309/shops"
API_VERSION = "202309"

TOKENS_FILE = "tiktok_tokens.json"

REQUEST_TIMEOUT_SECONDS = 15


class TikTokAuthError(RuntimeError):
    """Every message raised as this type is safe to display to a user: it
    is built only from fixed text, HTTP status codes, TikTok's own numeric
    error code / message / request_id, and exception class names — never
    from a URL, credential, token, code, or cipher."""


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def get_credentials() -> Tuple[str, str]:
    """Read (app_key, app_secret) from os.environ.

    Raises:
        TikTokAuthError: if either is missing. Only the variable NAMES are
        mentioned, never values.
    """
    app_key = os.environ.get("TIKTOK_APP_KEY", "").strip()
    app_secret = os.environ.get("TIKTOK_APP_SECRET", "").strip()

    missing = []
    if not app_key:
        missing.append("TIKTOK_APP_KEY")
    if not app_secret:
        missing.append("TIKTOK_APP_SECRET")
    if missing:
        raise TikTokAuthError(
            f"Missing credential(s): {', '.join(missing)}. "
            "Set them in Streamlit Cloud → Settings → Secrets."
        )
    return app_key, app_secret


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _request_json(url: str, params: Dict, headers: Dict, what: str) -> Dict:
    """GET url and return the parsed JSON body, raising only TikTokAuthError
    with safe messages.

    `url` and `params` frequently contain secrets (app_secret, auth_code,
    sign), so neither is ever logged or included in an error message, and
    the underlying requests exception — whose text includes the full URL —
    is never re-raised or chained (`from None`).
    """
    logger.info("TikTok request: %s", what)
    try:
        response = requests.get(
            url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except requests.Timeout:
        raise TikTokAuthError(
            f"{what}: TikTok did not respond within {REQUEST_TIMEOUT_SECONDS} seconds."
        ) from None
    except requests.RequestException as e:
        raise TikTokAuthError(
            f"{what}: network error reaching TikTok ({type(e).__name__})."
        ) from None

    logger.info("TikTok response status: %s (%s)", response.status_code, what)

    try:
        body = response.json()
    except ValueError:
        raise TikTokAuthError(
            f"{what}: TikTok returned a non-JSON response (HTTP {response.status_code})."
        ) from None

    if not isinstance(body, dict):
        raise TikTokAuthError(
            f"{what}: unexpected response shape from TikTok (HTTP {response.status_code})."
        )

    # TikTok reports application-level failures with a non-zero `code`
    # (usually alongside HTTP 200).
    if body.get("code") != 0:
        raise TikTokAuthError(_describe_api_error(what, body, response.status_code))

    if response.status_code >= 400:
        raise TikTokAuthError(f"{what}: HTTP {response.status_code} from TikTok.")

    return body


def _describe_api_error(what: str, body: Dict, http_status: int) -> str:
    code = body.get("code", "unknown")
    message = str(body.get("message", "") or "no message")[:200]
    request_id = str(body.get("request_id", "") or "")[:64]
    detail = f" (request_id={request_id})" if request_id else ""
    return f"{what}: TikTok API error [code={code}, HTTP {http_status}]: {message}{detail}"


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

def exchange_code_for_token(auth_code: str) -> Dict:
    """Exchange a one-time authorization code for access + refresh tokens.

    GET https://auth.tiktok-shops.com/api/v2/token/get
        ?app_key=...&app_secret=...&auth_code=...&grant_type=authorized_code

    Args:
        auth_code: the `code` query parameter TikTok appended to the
                   /tiktok_callback redirect. One-time use.

    Returns:
        TikTok's `data` dict (access_token, access_token_expire_in,
        refresh_token, refresh_token_expire_in, open_id, seller_name,
        seller_base_region, user_type, granted_scopes, ...).
        THIS DICT CONTAINS SECRETS — do not print or display it.

    Raises:
        TikTokAuthError: missing credentials/code, network failure, or a
        TikTok-side error.
    """
    if not auth_code or not str(auth_code).strip():
        raise TikTokAuthError("Missing authorization code.")

    app_key, app_secret = get_credentials()

    body = _request_json(
        AUTH_HOST + TOKEN_GET_PATH,
        params={
            "app_key": app_key,
            "app_secret": app_secret,
            "auth_code": str(auth_code).strip(),
            "grant_type": "authorized_code",
        },
        headers={},
        what="Token exchange",
    )

    data = body.get("data")
    if not isinstance(data, dict) or not data.get("access_token") or not data.get("refresh_token"):
        # Field NAMES only — never values.
        received = sorted(data.keys()) if isinstance(data, dict) else type(data).__name__
        raise TikTokAuthError(
            "Token exchange: response did not include access_token / refresh_token "
            f"(fields received: {received})."
        )

    logger.info("TikTok token exchange successful.")
    return data


# ---------------------------------------------------------------------------
# Request signing (TikTok Shop Open API, non-auth endpoints)
# ---------------------------------------------------------------------------

def generate_signature(path: str, query_params: Dict, app_secret: str, body: str = "") -> str:
    """Generate the `sign` value for a TikTok Shop Open API request.

    Per TikTok Shop's request-signing recipe:
        1. Take all query parameters EXCEPT `sign` and `access_token`.
        2. Sort them by key, ascending.
        3. Concatenate as key+value with no separators.
        4. Prepend the request path; append the request body, if any.
        5. Wrap the whole string with app_secret on both ends.
        6. HMAC-SHA256 it, keyed with app_secret; lowercase hex digest.

    This is the API request signature, NOT the webhook signature (which is
    a different scheme entirely).

    app_secret is used as key material only — never logged or returned.
    """
    signable = {k: v for k, v in query_params.items() if k not in ("sign", "access_token")}
    concatenated = "".join(f"{k}{signable[k]}" for k in sorted(signable))
    string_to_sign = f"{app_secret}{path}{concatenated}{body}{app_secret}"
    return hmac.new(
        app_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# Authorized shops
# ---------------------------------------------------------------------------

def get_authorized_shops(access_token: str) -> List[Dict]:
    """Retrieve the shops this seller authorized for the app.

    GET https://open-api.tiktokglobalshop.com/authorization/202309/shops

    Signed request; the access token travels in the `x-tts-access-token`
    header (not the query string, so it never appears in a URL).

    Returns:
        List of shop dicts. Each has id, name, region, seller_type, code
        and cipher. THE `cipher` (shop_cipher) IS SENSITIVE — do not print
        or display it.

    Raises:
        TikTokAuthError: missing credentials/token, network failure, a
        TikTok-side error, or no shops returned.
    """
    if not access_token:
        raise TikTokAuthError("Missing access token for authorized-shops lookup.")

    app_key, app_secret = get_credentials()

    params = {
        "app_key": app_key,
        "timestamp": int(time.time()),
    }
    params["sign"] = generate_signature(AUTHORIZED_SHOPS_PATH, params, app_secret)

    body = _request_json(
        API_HOST + AUTHORIZED_SHOPS_PATH,
        params=params,
        headers={
            "x-tts-access-token": access_token,
            "content-type": "application/json",
        },
        what="Authorized shops lookup",
    )

    data = body.get("data")
    shops = data.get("shops") if isinstance(data, dict) else None
    if not isinstance(shops, list) or not shops:
        raise TikTokAuthError(
            "Authorized shops lookup: TikTok returned no authorized shops. "
            "Check that the seller account authorized the app."
        )

    logger.info("TikTok authorized shops retrieved: %s shop(s).", len(shops))
    return [s for s in shops if isinstance(s, dict)]


# ---------------------------------------------------------------------------
# Local token storage (interim — see module docstring)
# ---------------------------------------------------------------------------

def save_tokens(token_data: Dict, shops: List[Dict]) -> None:
    """Write tokens + authorized shops (including each shop's cipher) to
    TOKENS_FILE, with owner-only permissions (0600), atomically.

    Interim storage only: the file is wiped on every Streamlit Community
    Cloud restart, and it must never be committed to git.

    Raises:
        TikTokAuthError: if the file cannot be written. Message is generic.
    """
    record = {
        "access_token": token_data.get("access_token"),
        "access_token_expire_in": token_data.get("access_token_expire_in"),
        "refresh_token": token_data.get("refresh_token"),
        "refresh_token_expire_in": token_data.get("refresh_token_expire_in"),
        "open_id": token_data.get("open_id"),
        "seller_name": token_data.get("seller_name"),
        "seller_base_region": token_data.get("seller_base_region"),
        "user_type": token_data.get("user_type"),
        "granted_scopes": token_data.get("granted_scopes"),
        "shops": [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "region": s.get("region"),
                "seller_type": s.get("seller_type"),
                "code": s.get("code"),
                "cipher": s.get("cipher"),
            }
            for s in shops
        ],
        "fetch_time": int(time.time()),
    }

    tmp_path = TOKENS_FILE + ".tmp"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, indent=2)
        os.replace(tmp_path, TOKENS_FILE)
    except OSError as e:
        raise TikTokAuthError(
            f"Token save failed: could not write {TOKENS_FILE} ({type(e).__name__})."
        ) from None


# ---------------------------------------------------------------------------
# High-level entry point (called by pages/tiktok_callback.py)
# ---------------------------------------------------------------------------

def handle_oauth_callback(auth_code: str) -> Dict:
    """Full seller OAuth callback flow: exchange code → look up authorized
    shops → save tokens locally.

    Returns a SAFE summary suitable for display. It deliberately excludes
    the auth code, app secret, access token, refresh token, and shop_cipher:

        {
          "seller_name":        str | None,
          "seller_base_region": str | None,
          "granted_scopes":     list[str],
          "missing_scopes":     list[str],   # EXPECTED_SCOPES not reported granted
          "shops": [{"name", "region", "seller_type"}, ...],
        }

    Raises:
        TikTokAuthError: any failure, with a display-safe message.
    """
    token_data = exchange_code_for_token(auth_code)
    shops = get_authorized_shops(token_data["access_token"])
    save_tokens(token_data, shops)

    granted = token_data.get("granted_scopes")
    granted_scopes = [str(s) for s in granted] if isinstance(granted, list) else []
    # Only flag scopes as missing if TikTok actually reported a scope list —
    # an absent list means "unknown", not "nothing granted".
    missing_scopes = (
        [s for s in EXPECTED_SCOPES if s not in granted_scopes] if granted_scopes else []
    )

    return {
        "seller_name": token_data.get("seller_name"),
        "seller_base_region": token_data.get("seller_base_region"),
        "granted_scopes": granted_scopes,
        "missing_scopes": missing_scopes,
        "shops": [
            {
                "name": s.get("name"),
                "region": s.get("region"),
                "seller_type": s.get("seller_type"),
            }
            for s in shops
        ],
    }