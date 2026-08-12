"""
shopee_api.py
=============
Shopee OpenAPI v2 order data retrieval for Onbie Packing System.

Responsible ONLY for communicating with Shopee's API and returning
Python data structures. Does NOT write to any file, database, or CSV.
Does NOT import or depend on Streamlit.

Reuses authentication and token management from shopee_auth.py:
    - get_valid_access_token()  — returns a valid access_token, refreshing if needed
    - load_tokens()             — provides shop_id and partner_id
    - get_credentials()         — provides partner_id and partner_key for signing

Protected API signature (different from OAuth/auth endpoints):
    Base string: {partner_id}{api_path}{timestamp}{access_token}{shop_id}
    This is the Shopee v2 "shop-level" variant required for all non-auth
    endpoints. It is implemented here separately from shopee_auth.py's
    public variant ({partner_id}{api_path}{timestamp}) to keep concerns
    separated.

Environment variables required (same as shopee_auth.py):
    SHOPEE_PARTNER_ID    integer partner ID
    SHOPEE_PARTNER_KEY   secret key string
"""

import hmac
import hashlib
import time
import logging
import requests
from typing import Dict, List, Optional, Any

import shopee_auth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SHOPEE_HOST = "https://partner.shopeemobile.com"

ORDER_LIST_PATH   = "/api/v2/order/get_order_list"
ORDER_DETAIL_PATH = "/api/v2/order/get_order_detail"

# Maximum orders per page allowed by Shopee v2.
ORDER_LIST_PAGE_SIZE = 100

# Timeout for all outbound requests (seconds).
REQUEST_TIMEOUT_SECONDS = 15

# Default fields to request in get_order_detail.
# These cover the information needed by the packing system.
# Pass a custom list to get_order_detail() to override.
DEFAULT_OPTIONAL_FIELDS = [
    "buyer_user_id",
    "buyer_username",
    "estimated_shipping_fee",
    "recipient_address",
    "actual_shipping_fee",
    "item_list",
]


# ---------------------------------------------------------------------------
# Protected-API signature
# ---------------------------------------------------------------------------

def _generate_protected_signature(
    partner_id: int,
    api_path: str,
    timestamp: int,
    access_token: str,
    shop_id: int,
    partner_key: str,
) -> str:
    """Generate HMAC-SHA256 signature for a Shopee v2 protected (shop-level) API call.

    Protected endpoints (all non-auth endpoints such as order APIs) require
    access_token and shop_id in the base string. This is different from the
    public variant used in shopee_auth.py for OAuth endpoints.

    Base string format (per Shopee OpenAPI v2 documentation):
        {partner_id}{api_path}{timestamp}{access_token}{shop_id}

    Args:
        partner_id:   integer partner ID
        api_path:     API path string, e.g. "/api/v2/order/get_order_list"
        timestamp:    Unix timestamp (integer seconds)
        access_token: current valid access token — NEVER logged
        shop_id:      integer shop ID
        partner_key:  secret key string — NEVER logged

    Returns:
        Hex-encoded HMAC-SHA256 digest string.
    """
    base_string = f"{partner_id}{api_path}{timestamp}{access_token}{shop_id}"
    signature = hmac.new(
        partner_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    # Base string contains access_token — NOT logged.
    # Signature itself is NOT logged to avoid replay risk.
    return signature


# ---------------------------------------------------------------------------
# Authenticated request helper
# ---------------------------------------------------------------------------

def _shopee_get(api_path: str, params: Dict) -> Dict:
    """Send an authenticated GET request to a Shopee v2 protected endpoint.

    Handles:
    - Fetching a valid access_token (auto-refreshes if expired).
    - Loading shop_id and partner_id from saved tokens.
    - Generating the correct protected-API HMAC-SHA256 signature.
    - Sending the request with correct query parameters.
    - Raising clear errors for network failures and HTTP errors.
    - Raising RuntimeError for Shopee application-level errors (HTTP 200 + error).

    Args:
        api_path: Shopee API path, e.g. "/api/v2/order/get_order_list"
        params:   Additional query parameters specific to the endpoint.
                  Do NOT include partner_id, timestamp, sign, access_token,
                  or shop_id — those are added automatically.

    Returns:
        The "response" sub-dict from Shopee's JSON body, i.e. data["response"].
        Callers should access order_list, order_sn_list, etc. from this dict.

    Raises:
        RuntimeError:          no saved tokens; Shopee application-level error.
        ValueError:            missing credentials in environment.
        requests.Timeout:      network request timed out.
        requests.HTTPError:    HTTP 4xx or 5xx from Shopee.
        requests.ConnectionError: network unreachable.
    """
    # Get a valid access token (refreshes automatically if near expiry).
    access_token = shopee_auth.get_valid_access_token()

    # Load shop_id and partner_id from saved tokens.
    tokens = shopee_auth.load_tokens()
    if tokens is None:
        raise RuntimeError(
            "No saved tokens found. Authorize Shopee via Connect Shopee first."
        )
    shop_id = int(tokens.get("shop_id", 0))
    if not shop_id:
        raise RuntimeError(
            "shop_id is missing from saved tokens. Re-authorize via Connect Shopee."
        )

    # Get partner credentials for signing.
    partner_id, partner_key = shopee_auth.get_credentials()
    timestamp = int(time.time())

    sign = _generate_protected_signature(
        partner_id, api_path, timestamp, access_token, shop_id, partner_key
    )

    # Build query string — auth params first, then caller's params.
    query_params = {
        "partner_id":   partner_id,
        "timestamp":    timestamp,
        "sign":         sign,
        "access_token": access_token,
        "shop_id":      shop_id,
    }
    query_params.update(params)

    url = f"{SHOPEE_HOST}{api_path}"

    logger.info("GET %s%s (shop_id=%s)", SHOPEE_HOST, api_path, shop_id)
    # access_token and sign are NOT logged.

    try:
        response = requests.get(
            url,
            params=query_params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        raise requests.Timeout(
            f"Network timeout: Shopee API did not respond within "
            f"{REQUEST_TIMEOUT_SECONDS} seconds. (path={api_path})"
        )
    except requests.ConnectionError as e:
        raise requests.ConnectionError(
            f"Network error reaching Shopee API (path={api_path}): {e}"
        )

    logger.info("Shopee response status: %s (path=%s)", response.status_code, api_path)

    if response.status_code == 401:
        raise requests.HTTPError(
            f"HTTP 401 for {api_path}: signature or access_token is invalid."
        )
    if response.status_code == 403:
        raise requests.HTTPError(
            f"HTTP 403 for {api_path}: access denied. "
            "Check app permissions or re-authorize."
        )
    if response.status_code == 500:
        raise requests.HTTPError(
            f"HTTP 500 from Shopee for {api_path}. Try again later."
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise requests.HTTPError(
            f"HTTP error from Shopee (status={response.status_code}, path={api_path}): {e}"
        )

    try:
        data = response.json()
    except Exception:
        raise RuntimeError(
            f"JSON parse failure from Shopee (path={api_path}). "
            f"Raw response (first 200 chars): {response.text[:200]!r}"
        )

    # Shopee returns HTTP 200 even for application-level errors.
    error_code = data.get("error", "")
    if error_code:
        error_msg = data.get("message", "no message")
        raise RuntimeError(
            f"Shopee API error [{error_code}] for {api_path}: {error_msg}"
        )

    response_data = data.get("response", {})
    logger.debug("Shopee response body keys: %s", list(data.keys()))
    return response_data


def _shopee_post(api_path: str, body: Dict) -> Dict:
    """Send an authenticated POST request to a Shopee v2 protected endpoint.

    Same authentication and error handling as _shopee_get(), but uses POST
    with a JSON body. Auth parameters go in the query string; business
    parameters go in the JSON body.

    Args:
        api_path: Shopee API path, e.g. "/api/v2/order/get_order_detail"
        body:     JSON body dict specific to the endpoint.
                  Do NOT include partner_id, shop_id, etc. — added automatically.

    Returns:
        The "response" sub-dict from Shopee's JSON body.
    """
    access_token = shopee_auth.get_valid_access_token()

    tokens = shopee_auth.load_tokens()
    if tokens is None:
        raise RuntimeError(
            "No saved tokens found. Authorize Shopee via Connect Shopee first."
        )
    shop_id = int(tokens.get("shop_id", 0))
    if not shop_id:
        raise RuntimeError(
            "shop_id is missing from saved tokens. Re-authorize via Connect Shopee."
        )

    partner_id, partner_key = shopee_auth.get_credentials()
    timestamp = int(time.time())

    sign = _generate_protected_signature(
        partner_id, api_path, timestamp, access_token, shop_id, partner_key
    )

    query_params = {
        "partner_id":   partner_id,
        "timestamp":    timestamp,
        "sign":         sign,
        "access_token": access_token,
        "shop_id":      shop_id,
    }

    url = f"{SHOPEE_HOST}{api_path}"

    logger.info("POST %s%s (shop_id=%s)", SHOPEE_HOST, api_path, shop_id)

    try:
        response = requests.post(
            url,
            params=query_params,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        raise requests.Timeout(
            f"Network timeout: Shopee API did not respond within "
            f"{REQUEST_TIMEOUT_SECONDS} seconds. (path={api_path})"
        )
    except requests.ConnectionError as e:
        raise requests.ConnectionError(
            f"Network error reaching Shopee API (path={api_path}): {e}"
        )

    logger.info("Shopee response status: %s (path=%s)", response.status_code, api_path)

    if response.status_code == 401:
        raise requests.HTTPError(
            f"HTTP 401 for {api_path}: signature or access_token is invalid."
        )
    if response.status_code == 403:
        raise requests.HTTPError(
            f"HTTP 403 for {api_path}: access denied. "
            "Check app permissions or re-authorize."
        )
    if response.status_code == 500:
        raise requests.HTTPError(
            f"HTTP 500 from Shopee for {api_path}. Try again later."
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise requests.HTTPError(
            f"HTTP error from Shopee (status={response.status_code}, path={api_path}): {e}"
        )

    try:
        data = response.json()
    except Exception:
        raise RuntimeError(
            f"JSON parse failure from Shopee (path={api_path}). "
            f"Raw response (first 200 chars): {response.text[:200]!r}"
        )

    error_code = data.get("error", "")
    if error_code:
        error_msg = data.get("message", "no message")
        raise RuntimeError(
            f"Shopee API error [{error_code}] for {api_path}: {error_msg}"
        )

    return data.get("response", {})


# ---------------------------------------------------------------------------
# Order list
# ---------------------------------------------------------------------------

def get_order_list(
    time_from: int,
    time_to: int,
    time_range_field: str = "create_time",
    order_status: Optional[str] = None,
    page_size: int = ORDER_LIST_PAGE_SIZE,
    cursor: str = "",
    response_optional_fields: Optional[List[str]] = None,
) -> Dict:
    """Retrieve one page of orders from Shopee.

    Calls GET /api/v2/order/get_order_list for a single page.
    For all pages, use get_all_orders() instead.

    Args:
        time_from:   Unix timestamp (int) — start of time range (inclusive).
        time_to:     Unix timestamp (int) — end of time range (inclusive).
                     Maximum range is 15 days per Shopee v2 spec.
        time_range_field: Which timestamp to filter by.
                     "create_time" (default) or "update_time".
        order_status: Optional filter. Common values:
                     "UNPAID", "READY_TO_SHIP", "PROCESSED", "SHIPPED",
                     "COMPLETED", "IN_CANCEL", "CANCELLED", "INVOICE_PENDING".
                     If None, all statuses are returned.
        page_size:   Number of orders per page. Max 100 (Shopee v2 limit).
        cursor:      Pagination cursor. Empty string ("") for the first page.
                     Use the next_cursor value from the previous response.
        response_optional_fields: Additional fields to include in each order
                     in the list response. If None, only order_sn is returned.
                     Example: ["order_status", "create_time", "update_time"]

    Returns:
        Dict with keys:
            order_list   — list of dicts, each containing at least "order_sn"
            next_cursor  — string cursor for the next page (empty if no more)
            more         — bool, True if more pages exist

    Raises:
        RuntimeError, ValueError, requests.* — see _shopee_get().
    """
    if int(time_to) - int(time_from) > 15 * 86400:
        raise ValueError(
            "Shopee order time range cannot exceed 15 days."
        )

    params = {
        "time_range_field": time_range_field,
        "time_from":        int(time_from),
        "time_to":          int(time_to),
        "page_size":        min(int(page_size), ORDER_LIST_PAGE_SIZE),
        "cursor":           cursor,
    }

    if order_status:
        params["order_status"] = order_status

    if response_optional_fields:
        params["response_optional_fields"] = ",".join(response_optional_fields)

    logger.info(
        "get_order_list: time_range_field=%s time_from=%s time_to=%s "
        "order_status=%s cursor=%r page_size=%s",
        time_range_field, time_from, time_to,
        order_status or "ALL", cursor or "(first page)", params["page_size"],
    )

    response = _shopee_get(ORDER_LIST_PATH, params)

    order_list  = response.get("order_list", [])
    next_cursor = response.get("next_cursor", "")
    more        = response.get("more", False)

    logger.info(
        "get_order_list: received %s orders, more=%s, next_cursor=%r",
        len(order_list), more, next_cursor,
    )

    return {
        "order_list":  order_list,
        "next_cursor": next_cursor,
        "more":        more,
    }


def get_all_orders(
    time_from: int,
    time_to: int,
    time_range_field: str = "create_time",
    order_status: Optional[str] = None,
    response_optional_fields: Optional[List[str]] = None,
) -> List[Dict]:
    """Retrieve ALL orders across all pages for a given time range.

    Calls get_order_list() repeatedly, following Shopee's cursor-based
    pagination, until no more pages remain.

    Args:
        time_from:   Unix timestamp (int) — start of time range.
        time_to:     Unix timestamp (int) — end of time range.
                     Maximum range is 15 days per Shopee v2 spec.
        time_range_field: "create_time" (default) or "update_time".
        order_status: Optional status filter. See get_order_list() for values.
        response_optional_fields: Optional list of extra fields per order.

    Returns:
        Flat list of all order dicts across all pages.
        Each dict contains at minimum "order_sn", plus any
        response_optional_fields that were requested.

    Raises:
        RuntimeError, ValueError, requests.* — propagated from get_order_list().
    """
    all_orders = []
    cursor     = ""
    page       = 1

    while True:
        logger.info("get_all_orders: fetching page %s (cursor=%r)", page, cursor)

        result = get_order_list(
            time_from=time_from,
            time_to=time_to,
            time_range_field=time_range_field,
            order_status=order_status,
            page_size=ORDER_LIST_PAGE_SIZE,
            cursor=cursor,
            response_optional_fields=response_optional_fields,
        )

        all_orders.extend(result["order_list"])
        logger.info(
            "get_all_orders: page %s yielded %s orders (total so far: %s)",
            page, len(result["order_list"]), len(all_orders),
        )

        if not result["more"]:
            break

        cursor = result["next_cursor"]
        if not cursor:
            # Defensive: if more=True but cursor is empty, stop to avoid an
            # infinite loop. This should not happen per Shopee spec.
            logger.warning(
                "get_all_orders: more=True but next_cursor is empty — stopping pagination."
            )
            break

        page += 1

    logger.info("get_all_orders: complete. Total orders fetched: %s", len(all_orders))
    return all_orders


# ---------------------------------------------------------------------------
# Order detail
# ---------------------------------------------------------------------------

def get_order_detail(
    order_sn_list: List[str],
    response_optional_fields: Optional[List[str]] = None,
) -> List[Dict]:
    """Retrieve full order details for a list of order_sn values.

    Calls POST /api/v2/order/get_order_detail.
    Shopee accepts a maximum of 50 order_sn values per call.
    If more than 50 are provided, this function batches them automatically.

    Args:
        order_sn_list: List of order_sn strings to retrieve details for.
                       Max 50 per Shopee v2 call; batching is handled internally.
        response_optional_fields: List of optional field names to include.
                       If None, DEFAULT_OPTIONAL_FIELDS is used:
                           buyer_user_id, buyer_username, estimated_shipping_fee,
                           recipient_address, actual_shipping_fee, item_list.
                       Pass an empty list [] to get only the default base fields.

    Returns:
        Flat list of order detail dicts, one per order.
        Each dict contains the base order fields plus any requested optional fields.

    Raises:
        ValueError:    order_sn_list is empty.
        RuntimeError, requests.* — see _shopee_post().
    """
    if not order_sn_list:
        raise ValueError("order_sn_list must contain at least one order_sn.")

    fields = response_optional_fields if response_optional_fields is not None \
        else DEFAULT_OPTIONAL_FIELDS

    # Shopee v2 limit: max 50 order_sn values per get_order_detail call.
    BATCH_SIZE = 50
    all_order_details = []

    for batch_start in range(0, len(order_sn_list), BATCH_SIZE):
        batch = order_sn_list[batch_start: batch_start + BATCH_SIZE]

        logger.info(
            "get_order_detail: requesting %s orders (batch %s–%s of %s)",
            len(batch),
            batch_start + 1,
            batch_start + len(batch),
            len(order_sn_list),
        )

        body = {"order_sn_list": batch}  # type: Dict[str, Any]
        if fields:
            body["response_optional_fields"] = ",".join(fields)

        response = _shopee_post(ORDER_DETAIL_PATH, body)

        order_list = response.get("order_list", [])

        # Log how many came back — but never log order contents that could
        # contain PII (buyer names, addresses).
        logger.info(
            "get_order_detail: received %s order details for batch of %s",
            len(order_list), len(batch),
        )

        all_order_details.extend(order_list)

    logger.info(
        "get_order_detail: complete. Total order details fetched: %s",
        len(all_order_details),
    )
    return all_order_details


# ---------------------------------------------------------------------------
# Convenience: list + detail in one call
# ---------------------------------------------------------------------------

def get_orders_with_detail(
    time_from: int,
    time_to: int,
    time_range_field: str = "create_time",
    order_status: Optional[str] = None,
    detail_optional_fields: Optional[List[str]] = None,
) -> List[Dict]:
    """Retrieve all orders AND their full detail for a given time range.

    Combines get_all_orders() and get_order_detail() into a single call:
    1. Fetch all order_sn values via get_all_orders().
    2. Fetch full detail for each order_sn via get_order_detail().

    Args:
        time_from:   Unix timestamp — start of time range.
        time_to:     Unix timestamp — end of time range.
        time_range_field: "create_time" (default) or "update_time".
        order_status: Optional status filter.
        detail_optional_fields: Optional fields for get_order_detail().
                     Defaults to DEFAULT_OPTIONAL_FIELDS if None.

    Returns:
        List of full order detail dicts. Empty list if no orders found.

    Raises:
        RuntimeError, ValueError, requests.* — propagated from sub-calls.
    """
    logger.info(
        "get_orders_with_detail: time_from=%s time_to=%s "
        "time_range_field=%s order_status=%s",
        time_from, time_to, time_range_field, order_status or "ALL",
    )

    orders = get_all_orders(
        time_from=time_from,
        time_to=time_to,
        time_range_field=time_range_field,
        order_status=order_status,
    )

    if not orders:
        logger.info("get_orders_with_detail: no orders found in time range.")
        return []

    order_sn_list = [o["order_sn"] for o in orders if o.get("order_sn")]
    logger.info(
        "get_orders_with_detail: fetching detail for %s orders.", len(order_sn_list)
    )

    return get_order_detail(
        order_sn_list=order_sn_list,
        response_optional_fields=detail_optional_fields,
    )