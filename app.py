import streamlit as st
import pandas as pd
import os
import streamlit.components.v1 as components
from datetime import datetime
from zoneinfo import ZoneInfo

st.set_page_config(page_title="Shopee Packing Checker", layout="wide")
st.title("📦 Shopee Packing Checker")

DATA_FILE = "data/orders_master.csv"
PACKED_FILE = "packed.csv"
SHOPEE_DATA_FILE = "data/shopee_orders.csv"
# Onbie's Shopee shop ID. Token/API calls are explicitly shop-scoped
# (shopee_auth / shopee_api take shop_id); Onbie is the only connected shop for now.
SHOPEE_ONBIE_SHOP_ID = 1272241861
# Shopee shops managed by this app: label -> shop_id. This is an ALLOWLIST:
# only shops listed here are synced into the packing queue. Onbie only for now.
# Every token/API call passes an explicit shop_id taken from this registry —
# there is no "latest token" / shop_id=None path.
SHOPEE_SHOPS = {"Onbie": SHOPEE_ONBIE_SHOP_ID}
SNAPSHOT_FILE = "packed_snapshots.csv"
SNAPSHOT_COLUMNS = [
    "order_number", "packed_at", "No. Pesanan", "Username (Pembeli)",
    "Nama Penerima", "Platform", "Toko", "Provinsi", "Kota/Kabupaten",
    "Antar ke counter/ pick-up", "Ekspedisi", "Nama Variasi", "Jumlah",
]

# ---- Business timestamps: explicit Asia/Jakarta (WIB) ----
# Streamlit Cloud runs in UTC, so a naive datetime.now() is 7 hours behind
# Jakarta time: packed_at would be stamped in UTC and the Daily Report's
# "today" would roll over at 07:00 WIB instead of midnight. Every business
# timestamp (packed_at, "today" in the Daily Report / Packing History, and
# the print stamps) comes from _now_wib(). Stored/printed format unchanged.
WIB = ZoneInfo("Asia/Jakarta")


def _now_wib():
    return datetime.now(WIB)


# ---- Shopee OAuth callback handler ----
# Runs once per page load. If Shopee redirected back here with ?code=&shop_id=,
# exchange the code for tokens immediately before rendering the packing UI.
# If no code is present, this block is skipped entirely — packing flow unaffected.
import shopee_auth as _shopee_auth

# ---- Inject Streamlit secrets into os.environ ----
# shopee_auth.py reads credentials from os.environ (keeping it Streamlit-free
# and independently testable). Here in app.py we bridge st.secrets → os.environ
# so that both local development (secrets.toml) and Streamlit Cloud (Secrets UI)
# work without a .env file or any other credential mechanism.
# This runs once at startup, before any shopee_auth function is called.
def _inject_shopee_secrets():
    try:
        partner_id  = str(st.secrets["SHOPEE_PARTNER_ID"]).strip()
        partner_key = str(st.secrets["SHOPEE_PARTNER_KEY"]).strip()
        os.environ["SHOPEE_PARTNER_ID"]  = partner_id
        os.environ["SHOPEE_PARTNER_KEY"] = partner_key
    except KeyError as e:
        # Secrets not configured yet — sidebar will show an error when user
        # tries to connect. Packing system continues working normally.
        pass

_inject_shopee_secrets()

# ---- Inject Supabase secrets into os.environ ----
# Same bridge pattern as _inject_shopee_secrets() above. shopee_auth.py's
# Supabase persistence layer (_get_supabase_client()) reads SUPABASE_URL /
# SUPABASE_KEY from os.environ, never from st.secrets directly, so it stays
# Streamlit-free. If these aren't configured, shopee_auth.py's Supabase
# functions all no-op and the app transparently falls back to tokens.json —
# nothing else needs to change or check for this.
def _inject_supabase_secrets():
    try:
        supabase_url = str(st.secrets["SUPABASE_URL"]).strip()
        supabase_key = str(st.secrets["SUPABASE_KEY"]).strip()
        os.environ["SUPABASE_URL"] = supabase_url
        os.environ["SUPABASE_KEY"] = supabase_key
    except KeyError:
        # Not configured — Shopee connection persistence falls back to
        # tokens.json (local dev) instead of surviving container restarts.
        pass

_inject_supabase_secrets()

# ---- Legacy fallback: bootstrap from Streamlit secrets if Supabase is down ----
# Supabase (see shopee_auth.py) is now the primary persistence layer and
# handles this automatically: load_tokens() checks Supabase first, and every
# save_tokens() call (OAuth connect, or a refresh rotating refresh_token)
# writes there immediately. This function only matters as a secondary
# safety net — e.g. Supabase is temporarily unreachable AND tokens.json is
# missing (fresh container) AND a `[shopee_tokens]` secret happens to be
# configured. It's a no-op whenever tokens.json OR Supabase already has
# tokens, which is the normal case once Supabase is set up.
def _bootstrap_shopee_tokens_from_secrets():
    if _shopee_auth.load_tokens(SHOPEE_ONBIE_SHOP_ID) is not None:
        return  # Supabase or tokens.json already has tokens — nothing to bootstrap

    try:
        shopee_secrets = st.secrets["shopee_tokens"]
        shop_id = shopee_secrets["shop_id"]
        refresh_token = shopee_secrets["refresh_token"]
    except KeyError:
        return  # no persisted tokens configured — normal until first Connect

    try:
        _shopee_auth.bootstrap_tokens_from_secrets(
            shop_id=shop_id,
            refresh_token=refresh_token,
        )
        # Force an immediate refresh so the sidebar shows "Terhubung" with a
        # real access token right away, instead of a stale/expired-looking
        # state until the first Shopee API call happens to trigger it.
        _shopee_auth.get_valid_access_token(SHOPEE_ONBIE_SHOP_ID)
    except Exception:
        # Best-effort bootstrap only — any failure (bad/rotated refresh
        # token, network issue, misconfigured secret) just falls back to
        # the normal "Belum terhubung ke Shopee" sidebar state, same as if
        # tokens.json had never existed.
        pass

_bootstrap_shopee_tokens_from_secrets()

def _handle_shopee_oauth():
    params = st.query_params
    code = params.get("code", "")
    shop_id_str = params.get("shop_id", "")

    if code and not shop_id_str:
        # Shopee returned a code but no shop_id (typically a merchant / main-account
        # authorization). That flow isn't supported here — say so instead of ignoring it.
        st.error(
            "❌ Shopee mengembalikan `code` tanpa `shop_id` (kemungkinan otorisasi akun "
            "merchant / main account). Flow ini belum didukung — tidak ada token yang disimpan."
        )
        return

    if not code or not shop_id_str:
        return  # normal page load, not an OAuth callback

    st.info("🔑 Shopee OAuth callback diterima. Menukar code untuk access token...")

    try:
        shop_id = int(shop_id_str)
        saved = _shopee_auth.handle_oauth_callback(code, shop_id)

        st.success("✅ Token Shopee berhasil didapat dan disimpan ke `tokens.json`!")
        st.json({
            "access_token":  _shopee_auth.mask_token(saved["access_token"]),
            "refresh_token": _shopee_auth.mask_token(saved["refresh_token"]),
            "expire_in":     saved["expire_in"],
            "fetch_time":    saved["fetch_time"],
            "shop_id":       saved["shop_id"],
        })

        # Tell the user which registered shop this authorization connected. The
        # rerun below wipes anything rendered now, so leave a one-time notice that
        # the sidebar shows on the next run.
        _connected_label = next(
            (_l for _l, _sid in SHOPEE_SHOPS.items() if _sid == saved["shop_id"]), None
        )
        if _connected_label:
            st.session_state["_shopee_oauth_notice"] = (
                "success", f"✅ {_connected_label} terhubung (shop_id {saved['shop_id']})."
            )
        else:
            st.session_state["_shopee_oauth_notice"] = (
                "warning",
                f"⚠️ Token untuk shop_id {saved['shop_id']} tersimpan, tapi shop ini belum "
                "terdaftar di SHOPEE_SHOPS — belum akan disync ke packing queue.",
            )

        # Clear the OAuth params from the URL so a page refresh doesn't
        # attempt to re-use the same code (codes are one-time use),
        # then rerun so the clean URL takes effect immediately.
        st.query_params.clear()
        st.rerun()

    except ValueError as e:
        # Credential/config error — show message but let packing UI continue.
        st.error(f"❌ Konfigurasi error: {e}")
    except RuntimeError as e:
        # Shopee API returned an application-level error — let packing UI continue.
        st.error(f"❌ Shopee API error: {e}")
    except Exception as e:
        # Unexpected error — show detail but let packing UI continue.
        st.error(f"❌ Unexpected error saat OAuth: {e}")

_handle_shopee_oauth()


def adapt_shopee_api_to_df(orders_with_detail, shop_name="", channel_service_types=None, shop_id=None):
    """Convert get_orders_with_detail() output into a DataFrame matching
    the column shape the existing packing UI expects (same columns as
    load_shopee_orders() / SHOPEE_DATA_FILE).

    One row per product item (1 variant = 1 row).
    Every row also carries an INTERNAL "Shop ID" column (shop_id argument) —
    used only to replace one shop's rows in the queue without touching another
    shop's rows. It is never shown in any user-facing table.
    Only confirmed-working Shopee API fields are mapped. recipient_address
    and buyer_username are confirmed and mapped below. note and
    package_list (tracking_number, fulfillment method) were added next and
    are also mapped below; any other optional field not yet requested in
    detail_optional_fields is left as an empty string.

    Args:
        orders_with_detail: list of raw order-detail dicts from
            get_orders_with_detail().
        shop_name: the connected shop's real name, from
            shopee_api.get_shop_info()'s "shop_name" field (fetched once
            per sync — see _get_shopee_shop_name() below). This is NOT
            part of the order-detail response itself (Shopee has no
            reason to echo your own shop's name back to you per-order),
            so it's passed in separately and applied to every row's
            "Toko" here. Defaults to "" if not available/fetched yet.
        channel_service_types: dict of {logistics_channel_id:
            service_type_identifier}, from shopee_api.get_channel_list()
            (fetched once per sync — see _get_shopee_channel_service_types()
            below). Used to derive "Antar ke counter/ pick-up" (the
            fulfillment method: "Jemput / Pick-up" vs "Antar ke counter")
            from each order's package_list — never hardcoded. Defaults to
            None, in which case every row falls back to "Antar ke counter".
            This is separate from "Ekspedisi", which is simply the raw
            shipping_carrier value (e.g. "SPX Sameday", "SPX Hemat") — the
            courier/service name, not the pickup-vs-dropoff method.

    Status mapping (Shopee API → internal packing status):
        READY_TO_SHIP → "Perlu Dikirim"   (packable)
        CANCELLED     → "Batal"
        IN_CANCEL     → "Batal"
        SHIPPED       → "Sedang Dikirim"
        COMPLETED     → "Selesai"
        anything else → passed through as-is
    """
    _STATUS_MAP = {
        "READY_TO_SHIP": "Perlu Dikirim",
        "PROCESSED":     "Perlu Dikirim",
        "CANCELLED":     "Batal",
        "IN_CANCEL":     "Batal",
        "SHIPPED":       "Sedang Dikirim",
        "COMPLETED":     "Selesai",
    }
    _COLS = [
        "No. Pesanan", "No. Resi", "Username (Pembeli)", "Nama Penerima",
        "Kota/Kabupaten", "Provinsi", "SKU Induk", "Nama Produk", "Nama Barang",
        "Nama Variasi", "Jumlah", "Berat (Kg)", "Status Pesanan",
        "Waktu Pesanan Dibuat", "Tenggat Pengiriman", "Antar ke counter/ pick-up",
        "Ekspedisi", "Catatan dari Pembeli", "Platform", "Toko", "Sumber",
        "Shop ID",
    ]

    rows = []
    for order in (orders_with_detail or []):
        order_sn   = str(order.get("order_sn", "")).strip()
        raw_status = str(order.get("order_status", "")).strip()
        status     = _STATUS_MAP.get(raw_status, raw_status)
        item_list  = order.get("item_list") or []

        # Order-level fields — confirmed working optional fields only
        buyer_username   = str(order.get("buyer_username", "") or "")
        recipient        = order.get("recipient_address") or {}
        nama_penerima    = str(recipient.get("name", "") or "")
        kota             = str(recipient.get("city", "") or "")
        provinsi         = str(recipient.get("state", "") or "")
        catatan_pembeli  = str(order.get("note", "") or "")
        ekspedisi        = str(order.get("shipping_carrier", "") or "")

        # Tracking number lives per-package (an order can be split into
        # multiple packages/tracking numbers). We only have one "No. Resi"
        # field, so take the first non-empty tracking_number and never
        # concatenate — never invent a value if none is present yet.
        no_resi = ""
        package_list = order.get("package_list")
        if isinstance(package_list, list):
            for pkg in package_list:
                if not isinstance(pkg, dict):
                    continue
                tracking = str(pkg.get("tracking_number", "") or "").strip()
                if tracking:
                    no_resi = tracking
                    break

        # Fulfillment method ("Antar ke counter" vs "Jemput / Pick-up") is
        # derived from the first package's logistics_channel_id, looked up
        # against channel_service_types (from shopee_api.get_channel_list()
        # — never hardcoded). same_day/instant service types mean the
        # courier picks up from the seller; anything else (including an
        # unrecognized/missing channel id) defaults to counter drop-off.
        metode_kirim = "Antar ke counter"
        if isinstance(package_list, list) and package_list and isinstance(package_list[0], dict):
            channel_id = package_list[0].get("logistics_channel_id")
            service_type = (channel_service_types or {}).get(channel_id, "")
            if service_type in ("same_day", "instant"):
                metode_kirim = "Jemput / Pick-up"

        if not item_list:
            rows.append({c: "" for c in _COLS})
            rows[-1].update({
                "No. Pesanan":       order_sn,
                "No. Resi":          no_resi,
                "Username (Pembeli)": buyer_username,
                "Nama Penerima":     nama_penerima,
                "Kota/Kabupaten":    kota,
                "Provinsi":          provinsi,
                "Antar ke counter/ pick-up": metode_kirim,
                "Ekspedisi":         ekspedisi,
                "Catatan dari Pembeli": catatan_pembeli,
                "Status Pesanan":    status,
                "Platform":          "Shopee",
                "Toko":              shop_name,
                "Sumber":            "Shopee API",
                "Shop ID":           shop_id if shop_id is not None else "",
                "Jumlah":            0,
            })
        else:
            for item in item_list:
                row = {c: "" for c in _COLS}
                row.update({
                    "No. Pesanan":       order_sn,
                    "No. Resi":          no_resi,
                    "Username (Pembeli)": buyer_username,
                    "Nama Penerima":     nama_penerima,
                    "Kota/Kabupaten":    kota,
                    "Provinsi":          provinsi,
                    "Antar ke counter/ pick-up": metode_kirim,
                    "Ekspedisi":         ekspedisi,
                    "Catatan dari Pembeli": catatan_pembeli,
                    "SKU Induk":         str(item.get("item_sku", "") or ""),
                    "Nama Produk":       str(item.get("item_name", "") or ""),
                    "Nama Barang":       str(item.get("item_name", "") or ""),
                    "Nama Variasi":      str(item.get("model_name", "") or ""),
                    "Jumlah":            int(item.get("model_quantity_purchased", 0) or 0),
                    "Status Pesanan":    status,
                    "Platform":          "Shopee",
                    "Toko":              shop_name,
                    "Sumber":            "Shopee API",
                    "Shop ID":           shop_id if shop_id is not None else "",
                })
                rows.append(row)

    return pd.DataFrame(rows, columns=_COLS) if rows else pd.DataFrame(columns=_COLS)


# ---- Automatic Shopee order sync (every 5 minutes while the app is open) ----
# _sync_one_shop() is the single per-shop sync implementation — both the
# manual "Sync Now" button below and the automatic timer call it, so there's
# only ever one place that talks to the Shopee API for this purpose (no
# duplicated fetch/dedup logic).
SHOPEE_AUTO_SYNC_INTERVAL_SECONDS = 5 * 60


def _normalize_shop_id_column(df):
    """Return a copy of df with the internal "Shop ID" column present and
    numeric. Rows from before multi-shop support (older shopee_orders.csv /
    session data) have no Shop ID — they were all Onbie's."""
    df = df.copy()
    if "Shop ID" not in df.columns:
        df["Shop ID"] = SHOPEE_ONBIE_SHOP_ID
    else:
        df["Shop ID"] = (
            pd.to_numeric(df["Shop ID"], errors="coerce")
            .fillna(SHOPEE_ONBIE_SHOP_ID)
            .astype("int64")
        )
    return df


def _read_persisted_shopee_queue():
    """The last persisted queue (SHOPEE_DATA_FILE) as a DataFrame, or None if
    there is none. Used only as the "current queue" when a sync runs in a
    fresh session, so another shop's rows are carried over instead of lost."""
    if not os.path.exists(SHOPEE_DATA_FILE):
        return None
    try:
        df = pd.read_csv(SHOPEE_DATA_FILE)
    except Exception:
        return None
    df.columns = [str(c).strip() for c in df.columns]
    for _c in df.columns:
        if df[_c].dtype == object:
            df[_c] = df[_c].fillna("")
    return df


def _get_shop_sync_status(shop_id):
    """Per-shop sync status kept in st.session_state (created on demand):
    last_attempt_ts, last_ok_ts, error, n_orders."""
    if "_shopee_shop_status" not in st.session_state:
        st.session_state["_shopee_shop_status"] = {}
    _all = st.session_state["_shopee_shop_status"]
    if shop_id not in _all:
        _all[shop_id] = {"last_attempt_ts": 0, "last_ok_ts": None, "error": None, "n_orders": None}
    return _all[shop_id]


def _get_shopee_shop_name(shop_id, label=""):
    """Fetch and cache a shop's real name via
    shopee_api.get_shop_info() (GET /api/v2/shop/get_shop_info), so "Toko"
    reflects the actual shop instead of staying blank. The shop name
    doesn't change between orders/syncs, so this is cached per shop in
    st.session_state and only re-fetched if not yet cached or a previous
    attempt failed — not fetched on every sync. If the name can't be
    fetched, the shop's registry label is used (not cached, so the real
    name is retried next sync) so Toko is never blank.
    """
    if "_shopee_shop_names" not in st.session_state:
        st.session_state["_shopee_shop_names"] = {}
    _names = st.session_state["_shopee_shop_names"]
    if _names.get(shop_id):
        return _names[shop_id]
    try:
        import shopee_api as _shopee_api_shop
        info = _shopee_api_shop.get_shop_info(shop_id=shop_id)
        shop_name = str(info.get("shop_name", "") or "").strip()
        if shop_name:
            _names[shop_id] = shop_name
            return shop_name
    except Exception:
        pass
    return label


def _get_shopee_channel_service_types(shop_id):
    """Fetch and cache a {logistics_channel_id: service_type_identifier}
    map via shopee_api.get_channel_list() (GET
    /api/v2/logistics/get_channel_list), so the fulfillment method
    ("Antar ke counter" vs "Jemput / Pick-up") can be derived per-package
    without ever hardcoding a channel ID. A shop's enabled channels
    rarely change, so this is cached per shop in st.session_state the same
    way _get_shopee_shop_name() is — only re-fetched if not yet cached or a
    previous attempt failed.
    """
    if "_shopee_channel_types" not in st.session_state:
        st.session_state["_shopee_channel_types"] = {}
    _cache = st.session_state["_shopee_channel_types"]
    if _cache.get(shop_id):
        return _cache[shop_id]
    try:
        import shopee_api as _shopee_api_channels
        info = _shopee_api_channels.get_channel_list(shop_id=shop_id)
        channel_list = info.get("logistics_channel_list", [])
        if not isinstance(channel_list, list):
            channel_list = []
        channel_map = {
            ch.get("logistics_channel_id"): str(ch.get("service_type_identifier", "") or "")
            for ch in channel_list
            if isinstance(ch, dict) and ch.get("logistics_channel_id") is not None
        }
        if channel_map:
            _cache[shop_id] = channel_map
        return channel_map
    except Exception:
        return {}


def _replace_shop_rows(shop_id, fresh_df):
    """Replace ONLY this shop's rows in the packing queue with fresh_df and
    persist the merged queue (st.session_state["shopee_orders_df"] +
    SHOPEE_DATA_FILE). Other shops' rows are never touched, so one shop's
    sync (or failure — this is only called on success) can't delete another
    shop's orders. Rows stay grouped in SHOPEE_SHOPS order."""
    shop_id = int(shop_id)
    current = st.session_state.get("shopee_orders_df")
    if current is None:
        current = _read_persisted_shopee_queue()
    fresh = fresh_df.copy()
    fresh["Shop ID"] = shop_id
    if current is None or len(current) == 0:
        kept = fresh.iloc[0:0]
    else:
        current = _normalize_shop_id_column(current)
        kept = current[current["Shop ID"] != shop_id]

    if kept.empty:
        merged = fresh
    elif fresh.empty:
        merged = kept
    else:
        merged = pd.concat([kept, fresh], ignore_index=True)
    merged = merged.reindex(columns=list(fresh.columns))

    _rank = {sid: i for i, sid in enumerate(SHOPEE_SHOPS.values())}
    merged = (
        merged.assign(_shop_rank=merged["Shop ID"].map(_rank).fillna(len(_rank)))
        .sort_values("_shop_rank", kind="stable")
        .drop(columns="_shop_rank")
        .reset_index(drop=True)
    )

    os.makedirs("data", exist_ok=True)
    merged.to_csv(SHOPEE_DATA_FILE, index=False)
    st.session_state["shopee_orders_df"] = merged
    st.cache_data.clear()

    # The same order number under two shops would break the one-order-number
    # = one-order assumption used by packing; flag it (shown in the sidebar).
    _shops_per_order = merged.groupby("No. Pesanan")["Shop ID"].nunique()
    st.session_state["_shopee_order_conflicts"] = sorted(
        str(_o) for _o in _shops_per_order[_shops_per_order > 1].index
    )


def _sync_one_shop(label, shop_id):
    """Fetch READY_TO_SHIP + PROCESSED orders for ONE shop, dedupe by
    order_sn (keep first occurrence), and replace that shop's rows in the
    packing queue (other shops' rows untouched). Records this shop's own
    sync timestamp/outcome in st.session_state so the sidebar can display it.

    Returns (success: bool, message: str).
    """
    import shopee_api as _shopee_api_sync
    import time as _time_sync

    _status = _get_shop_sync_status(shop_id)
    _status["last_attempt_ts"] = _time_sync.time()

    _time_to_sync   = int(_time_sync.time())
    _time_from_sync = _time_to_sync - 7 * 86400  # last 7 days

    try:
        _raw_rts = _shopee_api_sync.get_orders_with_detail(
            time_from=_time_from_sync,
            time_to=_time_to_sync,
            time_range_field="create_time",
            order_status="READY_TO_SHIP",
            detail_optional_fields=["item_list", "buyer_username", "recipient_address", "note", "shipping_carrier", "package_list"],
            shop_id=shop_id,
        )
        _raw_proc = _shopee_api_sync.get_orders_with_detail(
            time_from=_time_from_sync,
            time_to=_time_to_sync,
            time_range_field="create_time",
            order_status="PROCESSED",
            detail_optional_fields=["item_list", "buyer_username", "recipient_address", "note", "shipping_carrier", "package_list"],
            shop_id=shop_id,
        )
        # Deduplicate by order_sn — keep first occurrence
        _seen = set()
        _raw_orders = []
        for _o in (_raw_rts + _raw_proc):
            _sn = _o.get("order_sn", "")
            if _sn not in _seen:
                _seen.add(_sn)
                _raw_orders.append(_o)
        _synced_df = adapt_shopee_api_to_df(
            _raw_orders,
            shop_name=_get_shopee_shop_name(shop_id, label),
            channel_service_types=_get_shopee_channel_service_types(shop_id),
            shop_id=shop_id,
        )
        _replace_shop_rows(shop_id, _synced_df)
        _n = _synced_df["No. Pesanan"].nunique()
        _status["last_ok_ts"] = _time_sync.time()
        _status["error"] = None
        _status["n_orders"] = _n
        return True, f"✅ {_n} order READY_TO_SHIP di-load ke packing queue"
    except RuntimeError as _e:
        _msg = f"❌ Shopee API error: {_e}"
        _status["error"] = _msg
        return False, _msg
    except ValueError as _e:
        _msg = f"❌ Parameter error: {_e}"
        _status["error"] = _msg
        return False, _msg
    except Exception as _e:
        _msg = f"❌ Error: {_e}"
        _status["error"] = _msg
        return False, _msg


def _sync_shopee_orders_now():
    """Sync every CONNECTED shop in SHOPEE_SHOPS, each one independently
    (own token, own shop name/channel cache, own status; a failure in one
    shop never touches another shop's rows). Both the manual "Sync Now"
    button and the automatic timer's per-shop check use _sync_one_shop(), so
    there's only ever one place that talks to the Shopee API for this
    purpose.

    Returns (success: bool, message: str). With a single connected shop the
    message is exactly that shop's message; with several, one
    "Label: message" per shop joined by " | " (success = all shops ok).
    """
    _results = []
    for _label, _shop_id in SHOPEE_SHOPS.items():
        if _shopee_auth.load_tokens(_shop_id) is None:
            continue  # not connected — nothing to sync for this shop
        _ok, _msg = _sync_one_shop(_label, _shop_id)
        _results.append((_label, _ok, _msg))

    if not _results:
        return False, "❌ Shopee API error: No saved tokens found. Authorize Shopee via Connect Shopee first."
    if len(_results) == 1:
        return _results[0][1], _results[0][2]
    return (
        all(_ok for _, _ok, _ in _results),
        " | ".join(f"{_label}: {_msg}" for _label, _, _msg in _results),
    )


def _maybe_auto_sync_shopee_orders():
    """For each connected shop in SHOPEE_SHOPS, run _sync_one_shop() only if
    at least SHOPEE_AUTO_SYNC_INTERVAL_SECONDS have passed since THAT shop's
    last sync attempt (per-shop, so one shop's failure or success never
    changes another shop's schedule). This, plus the fragment's own
    run_every timer below, is what prevents duplicate API calls on every
    normal Streamlit rerun — a plain page interaction in between auto-sync
    ticks does not re-trigger a Shopee API call."""
    import time as _time_check
    for _label, _shop_id in SHOPEE_SHOPS.items():
        if _shopee_auth.load_tokens(_shop_id) is None:
            continue  # not connected — nothing to sync
        _last_attempt = _get_shop_sync_status(_shop_id)["last_attempt_ts"]
        if _time_check.time() - _last_attempt < SHOPEE_AUTO_SYNC_INTERVAL_SECONDS:
            continue  # interval not elapsed yet
        _sync_one_shop(_label, _shop_id)


@st.fragment(run_every=SHOPEE_AUTO_SYNC_INTERVAL_SECONDS)
def _shopee_auto_sync_fragment():
    """A st.fragment with run_every re-executes on its own timer while the
    browser tab stays open, independent of whether the user interacts with
    the app — this is what makes syncing "automatic" without a separate
    worker/service or hand-rolled JS polling. Renders nothing; it only
    performs the (rate-limited) sync check above."""
    _maybe_auto_sync_shopee_orders()


# ---- Shopee Integration sidebar ----
# Entirely in the sidebar so it never interferes with the packing UI layout.
# The packing checker works normally regardless of Shopee connection status.
# One block per shop in SHOPEE_SHOPS (Onbie only for now); with a single shop
# the layout is the same as the original single-shop sidebar.
with st.sidebar:
    st.header("🟠 Shopee Integration")

    # Registers the run_every fragment so the 5-minute auto-sync timer
    # keeps ticking on every render of this sidebar (i.e. always).
    _shopee_auto_sync_fragment()

    # One-time notice left by the OAuth callback (it reruns right after
    # saving tokens, which would wipe anything rendered there).
    _oauth_notice = st.session_state.pop("_shopee_oauth_notice", None)
    if _oauth_notice:
        getattr(st, _oauth_notice[0])(_oauth_notice[1])

    _multi_shop = len(SHOPEE_SHOPS) > 1
    _connected_shops = []

    for _shop_label, _shop_id in SHOPEE_SHOPS.items():
        _what = f"Shopee ({_shop_label})" if _multi_shop else "Shopee"
        if _multi_shop:
            st.markdown(f"**{_shop_label}**")

        _tokens = _shopee_auth.load_tokens(_shop_id)

        if _tokens:
            _connected_shops.append((_shop_label, _shop_id))
            st.success("✅ Shopee Terhubung")
            st.caption(f"Shop ID: {_tokens.get('shop_id', '-')}")

            # Show token expiry info if available
            _fetch_time = _tokens.get("fetch_time", 0)
            _expire_in  = _tokens.get("expire_in", 0)
            if _fetch_time and _expire_in:
                _expire_ts = _fetch_time + _expire_in
                _expire_dt = datetime.utcfromtimestamp(_expire_ts).strftime("%Y-%m-%d %H:%M UTC")
                st.caption(f"Token expires: {_expire_dt}")
                if _shopee_auth.is_token_expired(_tokens):
                    st.warning("⚠️ Token sudah expired atau hampir expired. Klik Reconnect.")

            with st.expander("Token Details"):
                st.json({
                    "access_token":  _shopee_auth.mask_token(_tokens.get("access_token", "")),
                    "refresh_token": _shopee_auth.mask_token(_tokens.get("refresh_token", "")),
                    "expire_in":     _tokens.get("expire_in"),
                    "fetch_time":    _tokens.get("fetch_time"),
                    "shop_id":       _tokens.get("shop_id"),
                })

            # Shopee rotates refresh_token on each refresh. The stored tokens
            # (Supabase, or tokens.json when Supabase isn't configured) always
            # have the current one, but Streamlit secrets can only be updated
            # manually (an app can't write to its own Secrets at runtime), so
            # flag it here when they've drifted apart — otherwise the NEXT
            # container restart would bootstrap from the now-stale secret value
            # and fail, silently landing back on "Belum terhubung ke Shopee".
            # The [shopee_tokens] secret is Onbie's only.
            if _shop_id == SHOPEE_ONBIE_SHOP_ID:
                try:
                    _secret_refresh_token = st.secrets["shopee_tokens"]["refresh_token"]
                    if _secret_refresh_token and _secret_refresh_token != _tokens.get("refresh_token"):
                        st.info(
                            "🔁 Refresh token sudah berubah sejak terakhir di-set di Secrets. "
                            "Update nilai `refresh_token` di Streamlit Cloud → Settings → Secrets "
                            "(lihat Token Details di atas) supaya koneksi tetap bertahan setelah restart berikutnya."
                        )
                except KeyError:
                    pass  # no [shopee_tokens] secret configured — nothing to compare

            if st.button(f"🔄 Reconnect {_what}", key=f"btn_reconnect_shopee_{_shop_id}"):
                try:
                    # Redirect URL must match exactly what is registered in Shopee Open Platform.
                    _redirect_url = "https://onbie-packing.streamlit.app"
                    _auth_url = _shopee_auth.generate_auth_url(_redirect_url)
                    st.link_button(f"🔄 Klik di sini untuk reconnect ke {_what}", _auth_url)
                except ValueError as e:
                    st.error(f"❌ {e}")

        else:
            st.warning("Belum terhubung ke Shopee")

            if st.button(f"🟠 Connect {_what}", key=f"btn_connect_shopee_{_shop_id}"):
                try:
                    # Redirect URL must match exactly what is registered in Shopee Open Platform.
                    _redirect_url = "https://onbie-packing.streamlit.app"
                    _auth_url = _shopee_auth.generate_auth_url(_redirect_url)
                    st.link_button(f"🟠 Klik di sini untuk connect ke {_what}", _auth_url)
                except ValueError as e:
                    st.error(f"❌ {e}")

            st.caption(
                "Klik tombol di atas untuk mengizinkan Onbie Packing System "
                "mengakses data order Shopee kamu."
            )

        if _multi_shop:
            st.divider()

    if _connected_shops:
        # ----------------------------------------------------------------
        # Phase 1 — Shopee direct packing queue sync
        # ----------------------------------------------------------------
        st.divider()
        if "shopee_orders_df" in st.session_state:
            _n_synced = st.session_state["shopee_orders_df"]["No. Pesanan"].nunique()
            st.success(f"✅ Packing queue: {_n_synced} order dari Shopee")

        _order_conflicts = st.session_state.get("_shopee_order_conflicts") or []
        if _order_conflicts:
            st.warning(
                "⚠️ Nomor pesanan yang sama muncul di lebih dari satu shop: "
                + ", ".join(_order_conflicts[:5])
                + (" …" if len(_order_conflicts) > 5 else "")
            )

        _all_shop_status = st.session_state.get("_shopee_shop_status", {})
        for _shop_label, _shop_id in _connected_shops:
            _shop_state = _all_shop_status.get(_shop_id) or {}
            _lbl = f" ({_shop_label})" if _multi_shop else ""
            _last_ok_ts = _shop_state.get("last_ok_ts")
            if _last_ok_ts:
                _last_sync_wib = datetime.fromtimestamp(_last_ok_ts, tz=ZoneInfo("Asia/Jakarta"))
                st.caption(
                    f"Last sync{_lbl}: "
                    + _last_sync_wib.strftime("%Y-%m-%d %H:%M:%S") + " WIB"
                )
            else:
                st.caption(f"Last sync{_lbl}: belum pernah")
            if _shop_state.get("error"):
                st.caption((f"{_shop_label}: " if _multi_shop else "") + _shop_state["error"])
        st.caption(f"Auto-sync setiap {SHOPEE_AUTO_SYNC_INTERVAL_SECONDS // 60} menit selama app terbuka.")

        if st.button("🔄 Sync Now", key="btn_sync_shopee_orders"):
            with st.spinner("Fetching READY_TO_SHIP orders..."):
                _success, _message = _sync_shopee_orders_now()
                if _success:
                    st.success(_message)
                else:
                    st.error(_message)

        # ----------------------------------------------------------------
        # END Phase 1
        # ----------------------------------------------------------------

    st.divider()

CANCELLED_KEYWORDS = ["batal", "cancel"]
# Only orders whose status CONTAINS this phrase may be packed
PACKABLE_KEYWORD = "perlu dikirim"


def is_cancelled_status(status_value):
    s = str(status_value).strip().lower()
    return any(k in s for k in CANCELLED_KEYWORDS)


def is_packable_status(status_value):
    s = str(status_value).strip().lower()
    return (PACKABLE_KEYWORD in s) and not is_cancelled_status(status_value)


def big_banner(lines, bg_color):
    """Large, hard-to-miss banner. lines[0] is the headline, the rest are sub-lines."""
    parts = []
    for i, line in enumerate(lines):
        if i == 0:
            parts.append(
                f'<div style="font-size: 38px; font-weight: 900; letter-spacing: 2px;">{line}</div>'
            )
        else:
            parts.append(
                f'<div style="font-size: 18px; font-weight: 600; margin-top: 8px;">{line}</div>'
            )
    st.markdown(
        f"""
        <div style="
            background-color: {bg_color};
            color: white;
            text-align: center;
            padding: 28px;
            border-radius: 12px;
            margin-bottom: 16px;
        ">
            {''.join(parts)}
        </div>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data
def load_orders():
    if not os.path.exists(DATA_FILE):
        st.error(f"❌ Data file not found: {DATA_FILE}")
        return pd.DataFrame()
    df = pd.read_csv(DATA_FILE)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def load_shopee_orders():
    """Load the last Shopee-synced orders from SHOPEE_DATA_FILE.
    Returns an empty DataFrame with the correct columns if the file
    does not exist (i.e. no sync has been performed yet)."""
    _COLS = [
        "No. Pesanan", "No. Resi", "Username (Pembeli)", "Nama Penerima",
        "Kota/Kabupaten", "Provinsi", "SKU Induk", "Nama Produk", "Nama Barang",
        "Nama Variasi", "Jumlah", "Berat (Kg)", "Status Pesanan",
        "Waktu Pesanan Dibuat", "Tenggat Pengiriman", "Antar ke counter/ pick-up",
        "Ekspedisi", "Catatan dari Pembeli", "Platform", "Toko", "Sumber",
        "Shop ID",
    ]
    if not os.path.exists(SHOPEE_DATA_FILE):
        return pd.DataFrame(columns=_COLS)
    df = pd.read_csv(SHOPEE_DATA_FILE)
    df.columns = [str(c).strip() for c in df.columns]
    # Older files have no Shop ID (all rows were Onbie's) — fill it in.
    return _normalize_shop_id_column(df)


@st.cache_data
def load_packed_df():
    if os.path.exists(PACKED_FILE):
        df = pd.read_csv(PACKED_FILE)
        df["order_number"] = df["order_number"].astype(str).str.strip()
        if "packed_at" not in df.columns:
            df["packed_at"] = ""  # old-format file, no timestamp known
        return df
    return pd.DataFrame(columns=["order_number", "packed_at"])


def load_packed_orders():
    return set(load_packed_df()["order_number"])


def get_packed_at(order_number):
    df = load_packed_df()
    match = df[df["order_number"] == str(order_number).strip()]
    if not match.empty:
        val = match.iloc[0]["packed_at"]
        return val if str(val).strip() else None
    return None


@st.cache_data
def load_snapshots_df():
    if os.path.exists(SNAPSHOT_FILE):
        df = pd.read_csv(SNAPSHOT_FILE)
        df["order_number"] = df["order_number"].astype(str).str.strip()
        return df
    return pd.DataFrame(columns=SNAPSHOT_COLUMNS)


def save_packed_snapshot(order_number, order_rows, packed_at):
    """Save a permanent snapshot of this order's product rows at pack time,
    so the Daily Packing Report keeps working even after orders_df has since
    moved on (a later Shopee sync, etc.). One row is saved per
    product row so multi-item orders are preserved. Never deletes old
    snapshots (no retention/pruning) and never duplicates an order that
    already has a snapshot."""
    if order_rows is None or order_rows.empty:
        return
    existing = load_snapshots_df()
    if order_number in set(existing["order_number"]):
        return  # already snapshotted, don't duplicate
    new_rows = pd.DataFrame({
        "order_number": order_number,
        "packed_at": packed_at,
        "No. Pesanan": order_rows.get("No. Pesanan", "-"),
        "Username (Pembeli)": order_rows.get("Username (Pembeli)", "-"),
        "Nama Penerima": order_rows.get("Nama Penerima", "-"),
        "Platform": order_rows.get("Platform", "-"),
        "Toko": order_rows.get("Toko", "-"),
        "Provinsi": order_rows.get("Provinsi", "-"),
        "Kota/Kabupaten": order_rows.get("Kota/Kabupaten", "-"),
        "Antar ke counter/ pick-up": order_rows.get("Antar ke counter/ pick-up", "-"),
        "Ekspedisi": order_rows.get("Ekspedisi", "-"),
        "Nama Variasi": order_rows.get("Nama Variasi", "-"),
        "Jumlah": order_rows.get("Jumlah", 0),
    })
    combined = pd.concat([existing, new_rows], ignore_index=True)
    combined.to_csv(SNAPSHOT_FILE, index=False)
    load_snapshots_df.clear()  # invalidate cache: file just changed on disk


def save_packed_order(order_number, order_rows=None):
    df = load_packed_df()
    order_number = str(order_number).strip()
    packed_at = _now_wib().strftime("%Y-%m-%d %H:%M:%S")
    if order_number not in set(df["order_number"]):
        new_row = pd.DataFrame([{
            "order_number": order_number,
            "packed_at": packed_at,
        }])
        df = pd.concat([df, new_row], ignore_index=True)
    df.to_csv(PACKED_FILE, index=False)
    load_packed_df.clear()  # invalidate cache: file just changed on disk
    save_packed_snapshot(order_number, order_rows, packed_at)


def style_dashboard_table(df, wrap_columns=None):
    """Style dataframe for dashboard report: center-align all columns, enable text wrapping for specified columns."""
    if wrap_columns is None:
        wrap_columns = []

    def get_center_style(val):
        return "text-align: center; vertical-align: middle;"

    def get_wrap_style(val):
        return "text-align: center; vertical-align: middle; white-space: pre-wrap; word-wrap: break-word; max-width: 180px; padding: 12px;"

    # Start with the base styler
    styler = df.style

    # Apply center alignment to all cells
    styler = styler.map(get_center_style)

    # Override with wrap styling for specific columns
    for col in wrap_columns:
        if col in df.columns:
            styler = styler.map(get_wrap_style, subset=[col])

    # Center-align headers with CSS
    styler = styler.set_uuid("packing_table")
    styler = styler.set_table_styles([
        {'selector': 'th', 'props': [('text-align', 'center'), ('vertical-align', 'middle'), ('padding', '12px'), ('font-weight', 'bold')]},
        {'selector': 'td', 'props': [('padding', '12px'), ('vertical-align', 'middle')]},
        {'selector': 'th, td', 'props': [('border', '1px solid #e0e0e0')]},
    ])

    return styler


def _safe_cell(val):
    """Render a table cell value safely: pandas/numpy NaN, None, or blank
    strings all fall back to '-' instead of literal 'nan' text."""
    if val is None:
        return "-"
    try:
        if pd.isna(val):
            return "-"
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return s if s and s.lower() != "nan" else "-"


def build_rowspan_rows_html(df, group_col, merge_cols, other_cols, blank_cols=None, right_align_cols=None):
    """Return concatenated <tr>...</tr> HTML for df, grouped by group_col:
    merge_cols get rowspan="{n}" on only the first row of each group;
    other_cols always get their own cell on every row (right-aligned if
    named in right_align_cols); blank_cols always render an empty <td></td>
    on every row (e.g. a printed "Keterangan" column meant to be filled in
    by hand). Shared by all three rowspan tables below — on-screen "Order
    Belum Diverifikasi", on-screen "Laporan Packing Hari Ini", and its
    print preview — so their grouping logic can't drift apart.
    """
    blank_cols = blank_cols or []
    right_align_cols = set(right_align_cols or [])
    rows = []
    for _, group in df.groupby(group_col, sort=False):
        n = len(group)
        for i, (_, r) in enumerate(group.iterrows()):
            cells = []
            if i == 0:
                for col in merge_cols:
                    cells.append(f'<td rowspan="{n}">{_safe_cell(r.get(col))}</td>')
            for col in other_cols:
                cls = ' class="rowspan-qty-cell"' if col in right_align_cols else ""
                cells.append(f"<td{cls}>{_safe_cell(r.get(col))}</td>")
            for _ in blank_cols:
                cells.append("<td></td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")
    return "".join(rows)


def focus_search_box():
    components.html(
        """
        <script>
        setTimeout(function() {
            const doc = window.parent.document;
            const inputs = doc.querySelectorAll('input[type="text"]');
            if (inputs.length > 0) {
                const box = inputs[inputs.length - 1];
                box.focus();
                box.select();
            }
        }, 150);
        </script>
        """,
        height=0,
    )


if "displayed_order" not in st.session_state:
    st.session_state.displayed_order = None
if "just_packed_order" not in st.session_state:
    st.session_state.just_packed_order = None
if "not_found_query" not in st.session_state:
    st.session_state.not_found_query = None


# Use Shopee-synced DataFrame when available (fast path, same session).
# On reload, fall back to the last persisted shopee_orders.csv sync.
if "shopee_orders_df" in st.session_state:
    orders_df = st.session_state["shopee_orders_df"]
else:
    orders_df = load_shopee_orders()

if orders_df.empty:
    st.warning("No orders loaded. Please check data/orders_master.csv")
else:
    # Fragment: a scan / print click reruns only this section, not the dashboard tables/chart below.
    # Anything that changes packed.csv must call st.rerun() (app scope) so the dashboard refreshes.
    @st.fragment
    def scan_and_order_section():
        # Re-resolve orders_df here: a fragment-only rerun does not re-execute the module-level lookup
        # above, and the 5-min auto-sync fragment may have replaced st.session_state["shopee_orders_df"]
        # since the last full run. Same resolution order as the module-level lookup.
        if "shopee_orders_df" in st.session_state:
            orders_df = st.session_state["shopee_orders_df"]
        else:
            orders_df = load_shopee_orders()

        packed_orders = load_packed_orders()

        with st.form("scan_form", clear_on_submit=True):
            search_query = st.text_input(
                "🔍 Scan / Cari No. Pesanan atau No. Resi",
                placeholder="Scan barcode, atau Enter kosong untuk konfirmasi pack...",
            )
            submitted = st.form_submit_button("Cari / Konfirmasi Pack")

        st.markdown(
            """
            <style>
            div[data-testid="stFormSubmitButton"] { display: none; }
            </style>
            """,
            unsafe_allow_html=True,
        )

        if submitted:
            q = str(search_query).strip()

            if q:
                # New scan: search and display the order
                mask = (
                    orders_df["No. Pesanan"].astype(str).str.contains(q, case=False, na=False)
                    | orders_df["No. Resi"].astype(str).str.contains(q, case=False, na=False)
                )
                results = orders_df[mask]

                if results.empty:
                    st.session_state.displayed_order = None
                    st.session_state.just_packed_order = None
                    st.session_state.not_found_query = q
                else:
                    order_number = str(results.iloc[0]["No. Pesanan"]).strip()
                    st.session_state.displayed_order = order_number
                    st.session_state.just_packed_order = None
                    st.session_state.not_found_query = None
            else:
                # Blank Enter = confirm pack the order currently on screen
                st.session_state.not_found_query = None
                order_number = st.session_state.displayed_order
                if order_number:
                    mask = orders_df["No. Pesanan"].astype(str).str.strip() == order_number
                    results = orders_df[mask]
                    if not results.empty:
                        status = results.iloc[0].get('Status Pesanan', '-')
                        packable = is_packable_status(status)
                        already_packed = order_number in packed_orders
                        if packable and not already_packed:
                            save_packed_order(order_number, results)
                            st.session_state.just_packed_order = order_number
                            st.rerun()  # full-app rerun: dashboard below must reflect the new pack

        # ---- Order not found banner ----
        if st.session_state.not_found_query:
            big_banner(["❌ ORDER TIDAK DITEMUKAN", "Cek nomor pesanan / nomor resi"], "#b71c1c")

        # ---- Render currently displayed order (persists across reruns) ----
        if st.session_state.displayed_order:
            order_number = st.session_state.displayed_order
            mask = orders_df["No. Pesanan"].astype(str).str.strip() == order_number
            results = orders_df[mask]

            if results.empty:
                st.session_state.displayed_order = None
            else:
                order_status = results.iloc[0].get('Status Pesanan', '-')
                cancelled = is_cancelled_status(order_status)
                packable = is_packable_status(order_status)
                packed_orders = load_packed_orders()  # refresh after possible packing above
                is_packed = order_number in packed_orders
                packed_at = get_packed_at(order_number) if is_packed else None

                if cancelled:
                    big_banner(["❌ PESANAN BATAL", "Jangan packing order ini"], "#b71c1c")
                elif st.session_state.just_packed_order == order_number:
                    big_banner(["✅ SUDAH DI-PACK", "Order ini berhasil dicatat"], "#2e7d32")
                elif is_packed:
                    ts_text = f"Packed At: {packed_at}" if packed_at else "Packed At: tidak tercatat"
                    big_banner(["✅ ORDER SUDAH DIVERIFIKASI", ts_text], "#2e7d32")
                elif packable:
                    big_banner(["🟢 PERLU DIKIRIM", "Order siap diverifikasi & di-pack"], "#2e7d32")
                else:
                    big_banner([f"STATUS: {order_status}", "Status bukan 'Perlu Dikirim' — tidak bisa di-pack"], "#757575")

                with st.container(border=True):
                    st.write(f"### 📦 Produk dalam order ini ({len(results)} item)")

                    for _, product in results.iterrows():
                        quantity = int(product.get('Jumlah', 0)) if pd.notna(product.get('Jumlah')) else 0
                        nama_produk = product.get('Nama Produk', '-')
                        nama_variasi = product.get('Nama Variasi', '-')
                        sku_induk_html = ""
                        if "SKU Induk" in results.columns:
                            sku_induk = product.get('SKU Induk', '-')
                            sku_induk_html = f"""
                                    <div style="font-size: 13px; color: #888; margin-top: 4px;">
                                        SKU: {sku_induk}
                                    </div>"""

                        st.markdown(
                            f"""
                            <div style="
                                border: 2px solid #e0e0e0;
                                border-radius: 12px;
                                padding: 24px;
                                margin-bottom: 16px;
                                background-color: #fafafa;
                                display: flex;
                                justify-content: space-between;
                                align-items: center;
                            ">
                                <div style="flex: 1; min-width: 0; padding-right: 16px;">
                                    <div style="font-size: 22px; font-weight: 700; color: #1a1a1a; line-height: 1.3;">
                                        {nama_produk}
                                    </div>
                                    <div style="font-size: 16px; color: #555; margin-top: 6px;">
                                        Variasi: <b>{nama_variasi}</b>
                                    </div>{sku_induk_html}
                                </div>
                                <div style="text-align: center; min-width: 110px;">
                                    <div style="font-size: 13px; color: #888; text-transform: uppercase; letter-spacing: 1px;">
                                        QTY
                                    </div>
                                    <div style="font-size: 48px; font-weight: 800; color: #d32f2f; line-height: 1;">
                                        {quantity}
                                    </div>
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

                    if cancelled or not packable:
                        st.button("🚫 Tidak Bisa Di-Pack", disabled=True, key="btn_blocked")
                    elif is_packed:
                        st.button("✅ Sudah Di-Pack", disabled=True, key="btn_already_packed")
                    else:
                        if st.button("📌 Mark as Packed", key="btn_manual_pack", use_container_width=True):
                            save_packed_order(order_number, results)
                            st.session_state.just_packed_order = order_number
                            st.rerun()

                    with st.expander("📋 Detail Order"):
                        d1, d2 = st.columns(2)
                        with d1:
                            st.write(f"**Order Number:** {order_number}")
                            st.write(f"**No. Resi:** {results.iloc[0].get('No. Resi', '-')}")
                            st.write(f"**Username:** {results.iloc[0].get('Username (Pembeli)', '-')}")
                            st.write(f"**Nama Penerima:** {results.iloc[0].get('Nama Penerima', '-')}")
                        with d2:
                            st.write(f"**Shop:** {results.iloc[0].get('Toko', '-')}")
                            st.write(f"**Kabupaten/Kota:** {results.iloc[0].get('Kota/Kabupaten', '-')}")
                            st.write(f"**Metode Kirim:** {results.iloc[0].get('Antar ke counter/ pick-up', '-')}")
                            st.write(f"**Ekspedisi:** {results.iloc[0].get('Ekspedisi', '-')}")
                            st.write(f"**Catatan Pembeli:** {results.iloc[0].get('Catatan dari Pembeli', '-')}")

                    # ---- Print this order only ----
                    row = results.iloc[0]
                    product_rows_html = "".join(
                        f"""
                        <tr>
                            <td>{row.get('No. Pesanan', '-')}</td>
                            <td>{row.get('Username (Pembeli)', '-')}</td>
                            <td>{row.get('Nama Penerima', '-')}</td>
                            <td>{row.get('Platform', '-')}</td>
                            <td>{row.get('Toko', '-')}</td>
                            <td>{row.get('Kota/Kabupaten', '-')}</td>
                            <td>{row.get('Antar ke counter/ pick-up', '-')}</td>
                            <td>{row.get('Ekspedisi', '-')}</td>
                            <td>{p.get('Nama Variasi', '-')}</td>
                            <td>{int(p.get('Jumlah', 0)) if pd.notna(p.get('Jumlah')) else 0}</td>
                        </tr>
                        """
                        for _, p in results.iterrows()
                    )

                    printable_html = f"""
                    <html>
                    <head>
                    <title>Order {order_number}</title>
                    <style>
                        body {{ font-family: Arial, sans-serif; padding: 24px; }}
                        h1 {{ font-size: 20px; }}
                        table {{ border-collapse: collapse; width: 100%; margin-top: 12px; font-size: 12px; }}
                        th {{ background:#f0f0f0; padding:8px; border:1px solid #ccc; text-align:center; vertical-align:middle; font-weight:bold; }}
                        td {{ padding:8px; border:1px solid #ccc; text-align:center; vertical-align:middle; }}
                    </style>
                    </head>
                    <body onload="window.print()">
                        <h1>📦 Packing Slip</h1>
                        <table>
                            <tr><th>No. Pesanan</th><th>Username</th><th>Nama Penerima</th><th>Platform</th><th>Toko</th><th>Kota/Kabupaten</th><th>Nama Logistik</th><th>Ekspedisi</th><th>Variasi</th><th>Qty</th></tr>
                            {product_rows_html}
                        </table>
                        <p style="margin-top:24px;font-size:12px;color:#888;">Dicetak: {_now_wib().strftime('%Y-%m-%d %H:%M:%S')}</p>
                    </body>
                    </html>
                    """

                    print_trigger = st.button("🖨️ Print Order Ini")
                    if print_trigger:
                        escaped = printable_html.replace("`", "\\`")
                        components.html(
                            f"""
                            <script>
                            const w = window.open('', '_blank');
                            w.document.write(`{escaped}`);
                            w.document.close();
                            </script>
                            """,
                            height=0,
                        )

        # Keep the scan box focused and ready for the next barcode
        focus_search_box()

    scan_and_order_section()

    # Tighter vertical gap here specifically (search area -> metrics),
    # via a low-margin <hr> instead of st.divider()'s default spacing.
    # Scoped to this one spot only — no global CSS, other dividers/sections
    # on the page are unaffected.
    st.markdown("<hr style='margin: 0.25rem 0;'>", unsafe_allow_html=True)

    # Use one row per unique order to avoid double-counting multi-product orders
    packed_orders = load_packed_orders()
    unique_orders = orders_df.drop_duplicates(subset="No. Pesanan").copy()
    unique_orders["__cancelled"] = unique_orders["Status Pesanan"].apply(is_cancelled_status)
    unique_orders["__packable"] = unique_orders["Status Pesanan"].apply(is_packable_status)
    unique_orders["__order_no_str"] = unique_orders["No. Pesanan"].astype(str).str.strip()
    unique_orders["__packed"] = unique_orders["__order_no_str"].isin(packed_orders)

    total_orders = len(unique_orders)
    total_cancelled = int(unique_orders["__cancelled"].sum())
    total_packed = int(unique_orders["__packed"].sum())
    packable_orders = int(unique_orders["__packable"].sum())
    packed_among_packable = int((unique_orders["__packable"] & unique_orders["__packed"]).sum())
    belum_diverifikasi = max(packable_orders - packed_among_packable, 0)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Order", total_orders)
    c2.metric("Perlu Dikirim", packable_orders)
    c3.metric("Batal", total_cancelled)
    c4.metric("Sudah Diverifikasi / Packed", total_packed)
    c5.metric("Belum Diverifikasi", belum_diverifikasi)

    # ---- Progress dashboard card ----
    st.subheader("📦 Progress Packing Hari Ini")

    if packable_orders > 0:
        progress_pct = packed_among_packable / packable_orders
    else:
        progress_pct = 0

    progress_pct_display = min(progress_pct, 1.0)
    st.progress(progress_pct_display)
    st.caption(f"{packed_among_packable} / {packable_orders} order selesai ({int(progress_pct * 100)}%)")

    # ---- Order Belum Diverifikasi ----
    st.divider()
    st.write("### 📋 Order Belum Diverifikasi")

    # Sourced from the full orders_df (one row per product/variant), not
    # unique_orders — unique_orders is deduped to one row per order for the
    # counts/metrics above, which would silently hide additional variants
    # on a multi-item order here. Order-level info (recipient, shop, etc.)
    # is already repeated on every variant row by adapt_shopee_api_to_df(),
    # so this is safe: 1 order with 2 variants -> 2 rows, each fully valid.
    belum_source = orders_df.copy()
    belum_source["__cancelled"] = belum_source["Status Pesanan"].apply(is_cancelled_status)
    belum_source["__packable"] = belum_source["Status Pesanan"].apply(is_packable_status)
    belum_source["__order_no_str"] = belum_source["No. Pesanan"].astype(str).str.strip()
    belum_source["__packed"] = belum_source["__order_no_str"].isin(packed_orders)

    belum_df = belum_source[belum_source["__packable"] & ~belum_source["__packed"]].copy()

    if belum_df.empty:
        st.success("Tidak ada order 'Perlu Dikirim' yang belum diverifikasi.")
    else:
        belum_display_df = belum_df.rename(columns={
            "No. Pesanan": "Order Number",
            "Username (Pembeli)": "Username",
            "Nama Penerima": "Recipient",
            "Platform": "Platform",
            "Toko": "Shop",
            "Kota/Kabupaten": "Kabupaten/Kota",
            "Antar ke counter/ pick-up": "Shipping",
            "Ekspedisi": "Ekspedisi",
            "Nama Variasi": "Variant",
            "Jumlah": "Qty",
        })[["Order Number", "Username", "Recipient", "Platform", "Shop", "Kabupaten/Kota", "Shipping", "Ekspedisi", "Variant", "Qty"]]

        # Display-only readability tweak for multi-variant orders: still
        # exactly 1 variant = 1 row, still a plain DataFrame (native
        # st.dataframe() below — sorting/select/copy/resize all keep
        # working). Rows are naturally already grouped by Order Number
        # (adapt_shopee_api_to_df() emits an order's item rows
        # consecutively), so for the 2nd+ row of the same order, blank the
        # order-level columns and show "↳" in Order Number instead of
        # repeating the same values. Variant/Qty stay untouched on every
        # row. A single-variant order is never marked as a duplicate, so
        # it's unaffected.
        belum_display_df = belum_display_df.copy()
        _belum_is_repeat_variant = belum_display_df["Order Number"].duplicated(keep="first")
        _belum_order_level_cols = ["Username", "Recipient", "Platform", "Shop", "Kabupaten/Kota", "Shipping", "Ekspedisi"]
        belum_display_df.loc[_belum_is_repeat_variant, _belum_order_level_cols] = ""
        belum_display_df.loc[_belum_is_repeat_variant, "Order Number"] = "↳"

        styled_belum_df = belum_display_df
        st.dataframe(
            styled_belum_df,
            use_container_width=True,
            hide_index=True,
        )

    # ---- Daily packing report (all orders packed today) ----
    st.divider()
    st.write("### 📅 Laporan Packing Hari Ini")

    packed_df = load_packed_df()
    today_str = _now_wib().strftime("%Y-%m-%d")
    today_packed_df = packed_df[packed_df["packed_at"].astype(str).str.startswith(today_str)]

    if today_packed_df.empty:
        st.info("Belum ada order yang di-pack hari ini.")
    else:
        today_order_numbers = set(today_packed_df["order_number"])

        # Read product/buyer details from the pack-time snapshot first, so
        # this report stays correct even if orders_df has since moved on
        # (new import / Shopee sync). Orders packed before a snapshot
        # exists for them fall back to the live orders_df lookup.
        snapshots_df = load_snapshots_df()
        snapshot_rows = snapshots_df[snapshots_df["order_number"].isin(today_order_numbers)]
        snapshotted_order_numbers = set(snapshot_rows["order_number"])
        missing_order_numbers = today_order_numbers - snapshotted_order_numbers

        report_rows = snapshot_rows.drop(columns=["order_number", "packed_at"])

        if missing_order_numbers:
            fallback_rows = orders_df[
                orders_df["No. Pesanan"].astype(str).str.strip().isin(missing_order_numbers)
            ]
            report_rows = pd.concat([report_rows, fallback_rows], ignore_index=True)

        st.write(f"**{len(today_order_numbers)} order** sudah di-pack hari ini ({today_str})")

        report_df = report_rows[
            ["No. Pesanan", "Username (Pembeli)", "Nama Penerima", "Platform", "Toko", "Kota/Kabupaten", "Antar ke counter/ pick-up", "Ekspedisi", "Nama Variasi", "Jumlah"]
        ].copy()

        styled_report_df = report_df.rename(columns={
            "No. Pesanan": "Order Number",
            "Username (Pembeli)": "Username",
            "Nama Penerima": "Recipient",
            "Platform": "Platform",
            "Toko": "Shop",
            "Kota/Kabupaten": "Kabupaten/Kota",
            "Antar ke counter/ pick-up": "Shipping",
            "Ekspedisi": "Ekspedisi",
            "Nama Variasi": "Variant",
            "Jumlah": "Qty"
        })

        # Display-only: same multi-variant readability tweak as "Order Belum
        # Diverifikasi" above. report_rows / report_df (used by the print HTML
        # and the snapshot/fallback logic) are NOT touched -- only this
        # on-screen copy. 1 variant = 1 row is preserved; for the 2nd+ row of
        # the same Order Number, blank the order-level columns and show "↳" in
        # Order Number. Variant/Qty stay on every row.
        report_display_df = styled_report_df.copy()
        _report_is_repeat_variant = report_display_df["Order Number"].duplicated(keep="first")
        _report_order_level_cols = ["Username", "Recipient", "Platform", "Shop", "Kabupaten/Kota", "Shipping", "Ekspedisi"]
        report_display_df.loc[_report_is_repeat_variant, _report_order_level_cols] = ""
        report_display_df.loc[_report_is_repeat_variant, "Order Number"] = "↳"
        styled_report_df = report_display_df
        st.dataframe(
            styled_report_df,
            use_container_width=True,
            hide_index=True,
        )

        # Build printable daily report HTML — order-level columns (No.
        # Pesanan, Username, Nama Penerima, Platform, Toko, Kabupaten/Kota,
        # Antar ke counter/pick-up, Ekspedisi) merged via rowspan across
        # consecutive rows of the same order; Variasi, Qty, Keterangan stay
        # on every row. Now built via the same shared build_rowspan_rows_html()
        # the on-screen table above uses, with its own print-oriented CSS
        # below (Arial/white, separate from the on-screen dark styling).
        _report_rows_for_print = report_rows.copy()
        _report_rows_for_print["Jumlah"] = _report_rows_for_print["Jumlah"].apply(
            lambda v: int(v) if pd.notna(v) else 0
        )
        report_table_rows = build_rowspan_rows_html(
            _report_rows_for_print,
            group_col="No. Pesanan",
            merge_cols=["No. Pesanan", "Username (Pembeli)", "Nama Penerima", "Platform", "Toko", "Kota/Kabupaten", "Antar ke counter/ pick-up", "Ekspedisi"],
            other_cols=["Nama Variasi", "Jumlah"],
            blank_cols=["Keterangan"],
        )

        daily_report_html = f"""
        <html>
        <head>
        <title>Laporan Packing {today_str}</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 24px; }}
            h1 {{ font-size: 20px; }}
            table {{ border-collapse: collapse; width: 100%; margin-top: 12px; font-size: 11px; }}
            th {{ background:#f0f0f0; padding:6px; border:1px solid #ccc; text-align:center; vertical-align:middle; font-weight:bold; }}
            td {{ padding:6px; border:1px solid #ccc; text-align:center; vertical-align:middle; }}
            .summary {{ margin-top: 16px; font-size: 14px; }}
        </style>
        </head>
        <body onload="window.print()">
            <h1>📅 Laporan Packing Harian — {today_str}</h1>
            <p class="summary">
                Total Order: {total_orders} &nbsp;|&nbsp;
                Sudah Di-Pack: {total_packed} &nbsp;|&nbsp;
                Perlu Dikirim: {packable_orders} &nbsp;|&nbsp;
                Batal: {total_cancelled}
            </p>
            <p class="summary"><b>Di-pack hari ini: {len(today_order_numbers)} order</b></p>
            <table>
                <tr>
                    <th>No. Pesanan</th><th>Username</th><th>Nama Penerima</th><th>Platform</th><th>Toko</th><th>Kabupaten/Kota</th><th>Nama Logistik</th><th>Ekspedisi</th><th>Variasi</th><th>Qty</th><th>Keterangan</th>
                </tr>
                {report_table_rows}
            </table>
            <p style="margin-top:24px;font-size:12px;color:#888;">Dicetak: {_now_wib().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </body>
        </html>
        """

        if st.button("🖨️ Print Laporan Hari Ini"):
            escaped_report = daily_report_html.replace("`", "\\`")
            components.html(
                f"""
                <script>
                const w = window.open('', '_blank');
                w.document.write(`{escaped_report}`);
                w.document.close();
                </script>
                """,
                height=0,
            )

    # ---- Packing History ----
    st.divider()
    st.write("### 📊 Packing History")

    packed_df = load_packed_df()

    # Parse timestamps
    packed_df_copy = packed_df.copy()
    packed_df_copy["packed_at"] = pd.to_datetime(packed_df_copy["packed_at"], errors='coerce')

    # Remove rows with invalid timestamps
    packed_df_valid = packed_df_copy[packed_df_copy["packed_at"].notna()].copy()

    if packed_df_valid.empty:
        st.info("Belum ada data history packing dengan timestamp.")
    else:
        # Calculate summary stats
        today = pd.Timestamp(_now_wib().date())
        seven_days_ago = today - pd.Timedelta(days=7)

        total_packed = len(packed_df_valid)
        today_packed = len(packed_df_valid[packed_df_valid["packed_at"].dt.date == today.date()])
        last_7_days = len(packed_df_valid[packed_df_valid["packed_at"] >= seven_days_ago])

        # Display summary metrics
        m1, m2, m3 = st.columns(3)
        m1.metric("Total Packed All Time", total_packed)
        m2.metric("Packed Today", today_packed)
        m3.metric("Packed Last 7 Days", last_7_days)

        # Daily breakdown
        st.subheader("Daily Breakdown")

        daily_counts = packed_df_valid.groupby(packed_df_valid["packed_at"].dt.date).size().reset_index()
        daily_counts.columns = ["Date", "Packed Count"]
        daily_counts = daily_counts.sort_values("Date", ascending=False)

        # Display table
        styled_daily = style_dashboard_table(daily_counts)
        st.dataframe(
            styled_daily,
            use_container_width=True,
            hide_index=True,
        )

        # Line chart (sorted by date ascending for better visualization)
        chart_data = daily_counts.sort_values("Date").copy()
        chart_data["Date"] = chart_data["Date"].astype(str)
        st.line_chart(chart_data.set_index("Date")["Packed Count"], use_container_width=True)