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
SNAPSHOT_FILE = "packed_snapshots.csv"
SNAPSHOT_COLUMNS = [
    "order_number", "packed_at", "No. Pesanan", "Username (Pembeli)",
    "Nama Penerima", "Platform", "Toko", "Provinsi", "Kota/Kabupaten",
    "Antar ke counter/ pick-up", "Nama Variasi", "Jumlah",
]

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
    if _shopee_auth.load_tokens() is not None:
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
        _shopee_auth.get_valid_access_token()
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


def adapt_shopee_api_to_df(orders_with_detail):
    """Convert get_orders_with_detail() output into a DataFrame matching
    the column shape the existing packing UI expects (same as orders_master.csv).

    One row per product item — mirrors the EasyBoss multi-row structure.
    Only confirmed-working Shopee API fields are mapped. recipient_address
    and buyer_username are confirmed and mapped below. note,
    shipping_carrier, and package_list (tracking_number) were added next
    and are also mapped below; any other optional field not yet requested
    in detail_optional_fields is left as an empty string.

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
        "Catatan dari Pembeli", "Platform", "Toko", "Sumber",
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
        metode_kirim     = str(order.get("shipping_carrier", "") or "")

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
                "Catatan dari Pembeli": catatan_pembeli,
                "Status Pesanan":    status,
                "Platform":          "Shopee",
                "Sumber":            "Shopee API",
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
                    "Catatan dari Pembeli": catatan_pembeli,
                    "SKU Induk":         str(item.get("item_sku", "") or ""),
                    "Nama Produk":       str(item.get("item_name", "") or ""),
                    "Nama Barang":       str(item.get("item_name", "") or ""),
                    "Nama Variasi":      str(item.get("model_name", "") or ""),
                    "Jumlah":            int(item.get("model_quantity_purchased", 0) or 0),
                    "Status Pesanan":    status,
                    "Platform":          "Shopee",
                    "Sumber":            "Shopee API",
                })
                rows.append(row)

    return pd.DataFrame(rows, columns=_COLS) if rows else pd.DataFrame(columns=_COLS)


# ---- Automatic Shopee order sync (every 5 minutes while the app is open) ----
# _sync_shopee_orders_now() is the single sync implementation — both the
# manual "Sync Now" button below and the automatic timer call this same
# function, so there's only ever one place that talks to the Shopee API for
# this purpose (no duplicated fetch/dedup logic).
SHOPEE_AUTO_SYNC_INTERVAL_SECONDS = 5 * 60


def _sync_shopee_orders_now():
    """Fetch READY_TO_SHIP + PROCESSED orders from Shopee, dedupe by
    order_sn (keep first occurrence), and refresh the packing queue —
    identical behavior to the original manual sync. Updates
    st.session_state["shopee_orders_df"], writes SHOPEE_DATA_FILE, clears
    the orders_master.csv cache, and records the sync timestamp/outcome in
    st.session_state so the sidebar can display it.

    Returns (success: bool, message: str).
    """
    import shopee_api as _shopee_api_sync
    import time as _time_sync

    _time_to_sync   = int(_time_sync.time())
    _time_from_sync = _time_to_sync - 7 * 86400  # last 7 days

    try:
        _raw_rts = _shopee_api_sync.get_orders_with_detail(
            time_from=_time_from_sync,
            time_to=_time_to_sync,
            time_range_field="create_time",
            order_status="READY_TO_SHIP",
            detail_optional_fields=["item_list", "buyer_username", "recipient_address", "note", "shipping_carrier", "package_list"],
        )
        _raw_proc = _shopee_api_sync.get_orders_with_detail(
            time_from=_time_from_sync,
            time_to=_time_to_sync,
            time_range_field="create_time",
            order_status="PROCESSED",
            detail_optional_fields=["item_list", "buyer_username", "recipient_address", "note", "shipping_carrier", "package_list"],
        )
        # Deduplicate by order_sn — keep first occurrence
        _seen = set()
        _raw_orders = []
        for _o in (_raw_rts + _raw_proc):
            _sn = _o.get("order_sn", "")
            if _sn not in _seen:
                _seen.add(_sn)
                _raw_orders.append(_o)
        _synced_df = adapt_shopee_api_to_df(_raw_orders)
        os.makedirs("data", exist_ok=True)
        _synced_df.to_csv(SHOPEE_DATA_FILE, index=False)
        st.session_state["shopee_orders_df"] = _synced_df
        st.cache_data.clear()
        _n = _synced_df["No. Pesanan"].nunique()
        st.session_state["_last_shopee_sync_ts"] = _time_sync.time()
        st.session_state["_last_shopee_sync_error"] = None
        return True, f"✅ {_n} order READY_TO_SHIP di-load ke packing queue"
    except RuntimeError as _e:
        _msg = f"❌ Shopee API error: {_e}"
        st.session_state["_last_shopee_sync_error"] = _msg
        return False, _msg
    except ValueError as _e:
        _msg = f"❌ Parameter error: {_e}"
        st.session_state["_last_shopee_sync_error"] = _msg
        return False, _msg
    except Exception as _e:
        _msg = f"❌ Error: {_e}"
        st.session_state["_last_shopee_sync_error"] = _msg
        return False, _msg


def _maybe_auto_sync_shopee_orders():
    """Run _sync_shopee_orders_now() only if Shopee is connected AND at
    least SHOPEE_AUTO_SYNC_INTERVAL_SECONDS have passed since the last
    successful sync. This, plus the fragment's own run_every timer below,
    is what prevents duplicate API calls on every normal Streamlit rerun —
    a plain page interaction in between auto-sync ticks does not re-trigger
    a Shopee API call."""
    if _shopee_auth.load_tokens() is None:
        return  # not connected — nothing to sync

    import time as _time_check
    _last_sync_ts = st.session_state.get("_last_shopee_sync_ts", 0)
    if _time_check.time() - _last_sync_ts < SHOPEE_AUTO_SYNC_INTERVAL_SECONDS:
        return  # interval not elapsed yet

    _sync_shopee_orders_now()


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
with st.sidebar:
    st.header("🟠 Shopee Integration")

    # Registers the run_every fragment so the 5-minute auto-sync timer
    # keeps ticking on every render of this sidebar (i.e. always).
    _shopee_auto_sync_fragment()

    _tokens = _shopee_auth.load_tokens()

    if _tokens:
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

        # Shopee rotates refresh_token on each refresh. tokens.json always
        # has the current one, but Streamlit secrets can only be updated
        # manually (an app can't write to its own Secrets at runtime), so
        # flag it here when they've drifted apart — otherwise the NEXT
        # container restart would bootstrap from the now-stale secret value
        # and fail, silently landing back on "Belum terhubung ke Shopee".
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

        if st.button("🔄 Reconnect Shopee", key="btn_reconnect_shopee"):
            try:
                # Redirect URL must match exactly what is registered in Shopee Open Platform.
                _redirect_url = "https://onbie-packing.streamlit.app"
                _auth_url = _shopee_auth.generate_auth_url(_redirect_url)
                st.link_button("🔄 Klik di sini untuk reconnect ke Shopee", _auth_url)
            except ValueError as e:
                st.error(f"❌ {e}")

        # ----------------------------------------------------------------
        # TEMPORARY SHOPEE INTEGRATION TEST
        # Proves real Shopee order data can flow from shopee_api into app.py.
        # Stored in st.session_state only — no CSV/database writes.
        # Remove this entire block when integration is promoted to production.
        # ----------------------------------------------------------------
        st.divider()
        st.caption("🧪 Shopee Integration Test")

        if st.button("🧪 Load Shopee Orders", key="btn_load_shopee_orders"):
            import shopee_api as _shopee_api
            import time as _time

            _time_to   = int(_time.time())
            _time_from = _time_to - 86400  # last 24 hours

            with st.spinner("Fetching orders from Shopee..."):
                try:
                    _orders = _shopee_api.get_orders_with_detail(
                        time_from=_time_from,
                        time_to=_time_to,
                        time_range_field="create_time",
                        detail_optional_fields=["item_list"],
                    )
                    st.session_state["_shopee_orders_test"] = _orders
                except ValueError as _e:
                    st.error(f"❌ Parameter error: {_e}")
                    st.session_state.pop("_shopee_orders_test", None)
                except RuntimeError as _e:
                    st.error(f"❌ Shopee API error: {_e}")
                    st.session_state.pop("_shopee_orders_test", None)
                except Exception as _e:
                    st.error(f"❌ Unexpected error: {_e}")
                    st.session_state.pop("_shopee_orders_test", None)

        # Display results if available in session state
        if "_shopee_orders_test" in st.session_state:
            _orders = st.session_state["_shopee_orders_test"]
            st.success(f"✅ {len(_orders)} order fetched dari Shopee (24 jam terakhir)")

            for _o in _orders:
                _sn     = _o.get("order_sn", "-")
                _status = _o.get("order_status", "-")
                _items  = _o.get("item_list", []) or []

                with st.expander(f"📦 {_sn}  —  {_status}"):
                    if not _items:
                        st.caption("(no item_list returned)")
                    for _item in _items:
                        st.json({
                            "item_name":  _item.get("item_name", "-"),
                            "model_name": _item.get("model_name", "-"),
                            "model_sku":  _item.get("model_sku", "-"),
                            "qty":        _item.get("model_quantity_purchased", "-"),
                        })
        # ----------------------------------------------------------------
        # END TEMPORARY SHOPEE INTEGRATION TEST
        # ----------------------------------------------------------------

        # ----------------------------------------------------------------
        # Phase 1 — Shopee direct packing queue sync
        # ----------------------------------------------------------------
        st.divider()
        if "shopee_orders_df" in st.session_state:
            _n_synced = st.session_state["shopee_orders_df"]["No. Pesanan"].nunique()
            st.success(f"✅ Packing queue: {_n_synced} order dari Shopee")

        _last_sync_ts = st.session_state.get("_last_shopee_sync_ts")
        if _last_sync_ts:
            _last_sync_wib = datetime.fromtimestamp(_last_sync_ts, tz=ZoneInfo("Asia/Jakarta"))
            st.caption(
                "Last sync: "
                + _last_sync_wib.strftime("%Y-%m-%d %H:%M:%S") + " WIB"
            )
        else:
            st.caption("Last sync: belum pernah")
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

        # ----------------------------------------------------------------
        # END TEMPORARY
        # ----------------------------------------------------------------

    else:
        st.warning("Belum terhubung ke Shopee")

        if st.button("🟠 Connect Shopee", key="btn_connect_shopee"):
            try:
                # Redirect URL must match exactly what is registered in Shopee Open Platform.
                _redirect_url = "https://onbie-packing.streamlit.app"
                _auth_url = _shopee_auth.generate_auth_url(_redirect_url)
                st.link_button("🟠 Klik di sini untuk connect ke Shopee", _auth_url)
            except ValueError as e:
                st.error(f"❌ {e}")

        st.caption(
            "Klik tombol di atas untuk mengizinkan Onbie Packing System "
            "mengakses data order Shopee kamu."
        )

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
        "Catatan dari Pembeli", "Platform", "Toko", "Sumber",
    ]
    if not os.path.exists(SHOPEE_DATA_FILE):
        return pd.DataFrame(columns=_COLS)
    df = pd.read_csv(SHOPEE_DATA_FILE)
    df.columns = [str(c).strip() for c in df.columns]
    return df


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


def load_snapshots_df():
    if os.path.exists(SNAPSHOT_FILE):
        df = pd.read_csv(SNAPSHOT_FILE)
        df["order_number"] = df["order_number"].astype(str).str.strip()
        return df
    return pd.DataFrame(columns=SNAPSHOT_COLUMNS)


def save_packed_snapshot(order_number, order_rows, packed_at):
    """Save a permanent snapshot of this order's product rows at pack time,
    so the Daily Packing Report keeps working even after orders_df has since
    moved on (new EasyBoss import, Shopee sync, etc.). One row is saved per
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
        "Nama Variasi": order_rows.get("Nama Variasi", "-"),
        "Jumlah": order_rows.get("Jumlah", 0),
    })
    combined = pd.concat([existing, new_rows], ignore_index=True)
    combined.to_csv(SNAPSHOT_FILE, index=False)


def save_packed_order(order_number, order_rows=None):
    df = load_packed_df()
    order_number = str(order_number).strip()
    packed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if order_number not in set(df["order_number"]):
        new_row = pd.DataFrame([{
            "order_number": order_number,
            "packed_at": packed_at,
        }])
        df = pd.concat([df, new_row], ignore_index=True)
    df.to_csv(PACKED_FILE, index=False)
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


# Shared CSS for the two ON-SCREEN rowspan tables ("Order Belum
# Diverifikasi" and "Laporan Packing Hari Ini"). Values reproduce
# st.dataframe()'s actual dark-theme rendering as verified against a real
# screenshot earlier (dark background, subtle borders, rounded outer
# corners, left-aligned text / right-aligned Qty, compact rows) — st.
# dataframe() itself can't rowspan, so this is a real HTML <table> instead,
# styled to match as closely as an HTML table can. The print preview uses
# its own separate print-oriented CSS (already in daily_report_html) and
# does not use this constant.
ONSCREEN_ROWSPAN_TABLE_STYLE = (
    "<style>"
    ".rowspan-order-wrapper{border:1px solid rgba(250,250,250,0.2);"
    "border-radius:8px;overflow:hidden;width:100%;}"
    ".rowspan-order-table{border-collapse:collapse;width:100%;"
    "background-color:#0e1117;color:#fafafa;font-size:14px;}"
    ".rowspan-order-table th{background-color:#262730;color:#fafafa;"
    "font-weight:600;text-align:left;padding:8px 14px;"
    "border:1px solid rgba(250,250,250,0.2);}"
    ".rowspan-order-table td{text-align:left;vertical-align:middle;"
    "padding:8px 14px;border:1px solid rgba(250,250,250,0.2);}"
    ".rowspan-order-table td.rowspan-qty-cell{text-align:right;}"
    "</style>"
)


def render_onscreen_rowspan_table(df, group_col, headers, merge_cols, other_cols, right_align_cols=None):
    """Build a full on-screen rowspan <table> (style + header + grouped
    body rows) using ONSCREEN_ROWSPAN_TABLE_STYLE. Pass the result to
    st.markdown(..., unsafe_allow_html=True)."""
    header_html = "".join(f"<th>{h}</th>" for h in headers)
    body_html = build_rowspan_rows_html(df, group_col, merge_cols, other_cols, right_align_cols=right_align_cols)
    return (
        ONSCREEN_ROWSPAN_TABLE_STYLE
        + '<div class="rowspan-order-wrapper">'
        + '<table class="rowspan-order-table"><tr>'
        + header_html
        + "</tr>"
        + body_html
        + "</table></div>"
    )


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
# EasyBoss/orders_master.csv is no longer used as the primary fallback.
if "shopee_orders_df" in st.session_state:
    orders_df = st.session_state["shopee_orders_df"]
else:
    orders_df = load_shopee_orders()

if orders_df.empty:
    st.warning("No orders loaded. Please check data/orders_master.csv")
else:
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
                                </div>
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
                        <tr><th>No. Pesanan</th><th>Username</th><th>Nama Penerima</th><th>Platform</th><th>Toko</th><th>Kota/Kabupaten</th><th>Nama Logistik</th><th>Variasi</th><th>Qty</th></tr>
                        {product_rows_html}
                    </table>
                    <p style="margin-top:24px;font-size:12px;color:#888;">Dicetak: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
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
            "Nama Variasi": "Variant",
            "Jumlah": "Qty",
        })[["Order Number", "Username", "Recipient", "Platform", "Shop", "Kabupaten/Kota", "Shipping", "Variant", "Qty"]]

        belum_table_html = render_onscreen_rowspan_table(
            belum_display_df,
            group_col="Order Number",
            headers=["Order Number", "Username", "Recipient", "Platform", "Shop", "Kabupaten/Kota", "Shipping", "Variant", "Qty"],
            merge_cols=["Order Number", "Username", "Recipient", "Platform", "Shop", "Kabupaten/Kota", "Shipping"],
            other_cols=["Variant", "Qty"],
            right_align_cols=["Qty"],
        )
        st.markdown(belum_table_html, unsafe_allow_html=True)

    # ---- Daily packing report (all orders packed today) ----
    st.divider()
    st.write("### 📅 Laporan Packing Hari Ini")

    packed_df = load_packed_df()
    today_str = datetime.now().strftime("%Y-%m-%d")
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
            ["No. Pesanan", "Username (Pembeli)", "Nama Penerima", "Platform", "Toko", "Kota/Kabupaten", "Antar ke counter/ pick-up", "Nama Variasi", "Jumlah"]
        ].copy()

        styled_report_df = report_df.rename(columns={
            "No. Pesanan": "Order Number",
            "Username (Pembeli)": "Username",
            "Nama Penerima": "Recipient",
            "Platform": "Platform",
            "Toko": "Shop",
            "Kota/Kabupaten": "Kabupaten/Kota",
            "Antar ke counter/ pick-up": "Shipping",
            "Nama Variasi": "Variant",
            "Jumlah": "Qty"
        })
        daily_report_onscreen_html = render_onscreen_rowspan_table(
            styled_report_df,
            group_col="Order Number",
            headers=["Order Number", "Username", "Recipient", "Platform", "Shop", "Kabupaten/Kota", "Shipping", "Variant", "Qty"],
            merge_cols=["Order Number", "Username", "Recipient", "Platform", "Shop", "Kabupaten/Kota", "Shipping"],
            other_cols=["Variant", "Qty"],
            right_align_cols=["Qty"],
        )
        st.markdown(daily_report_onscreen_html, unsafe_allow_html=True)

        # Build printable daily report HTML — order-level columns (No.
        # Pesanan, Username, Nama Penerima, Platform, Toko, Kabupaten/Kota,
        # Antar ke counter/pick-up) merged via rowspan across consecutive
        # rows of the same order; Variasi, Qty, Keterangan stay on every
        # row. Now built via the same shared build_rowspan_rows_html() the
        # on-screen table above uses, with its own print-oriented CSS below
        # (Arial/white, separate from the on-screen dark styling).
        _report_rows_for_print = report_rows.copy()
        _report_rows_for_print["Jumlah"] = _report_rows_for_print["Jumlah"].apply(
            lambda v: int(v) if pd.notna(v) else 0
        )
        report_table_rows = build_rowspan_rows_html(
            _report_rows_for_print,
            group_col="No. Pesanan",
            merge_cols=["No. Pesanan", "Username (Pembeli)", "Nama Penerima", "Platform", "Toko", "Kota/Kabupaten", "Antar ke counter/ pick-up"],
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
                    <th>No. Pesanan</th><th>Username</th><th>Nama Penerima</th><th>Platform</th><th>Toko</th><th>Kabupaten/Kota</th><th>Nama Logistik</th><th>Variasi</th><th>Qty</th><th>Keterangan</th>
                </tr>
                {report_table_rows}
            </table>
            <p style="margin-top:24px;font-size:12px;color:#888;">Dicetak: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
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
        today = pd.Timestamp(datetime.now().date())
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