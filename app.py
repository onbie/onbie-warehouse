import streamlit as st
import pandas as pd
import os
import streamlit.components.v1 as components
from datetime import datetime

st.set_page_config(page_title="Shopee Packing Checker", layout="wide")
st.title("📦 Shopee Packing Checker")

DATA_FILE = "data/orders_master.csv"
PACKED_FILE = "packed.csv"

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

# ---- Shopee Integration sidebar ----
# Entirely in the sidebar so it never interferes with the packing UI layout.
# The packing checker works normally regardless of Shopee connection status.
with st.sidebar:
    st.header("🟠 Shopee Integration")

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

        if st.button("🔄 Reconnect Shopee", key="btn_reconnect_shopee"):
            try:
                # Redirect URL must match exactly what is registered in Shopee Open Platform.
                _redirect_url = "https://onbie-packing.streamlit.app"
                _auth_url = _shopee_auth.generate_auth_url(_redirect_url)
                st.link_button("🔄 Klik di sini untuk reconnect ke Shopee", _auth_url)
            except ValueError as e:
                st.error(f"❌ {e}")

        # ----------------------------------------------------------------
        # TEMPORARY — remove after API testing is complete
        # ----------------------------------------------------------------
        st.divider()
        st.caption("🧪 Temporary API Test")
        if st.button("🧪 Test Shopee Order API", key="btn_test_shopee_api"):
            import shopee_api as _shopee_api
            import time as _time
            import datetime as _datetime

            _time_to   = int(_time.time())
            _time_from = _time_to - 86400  # last 24 hours

            try:
                _result = _shopee_api.get_order_list(
                    time_from=_time_from,
                    time_to=_time_to,
                    time_range_field="create_time",
                )
                _order_list = _result.get("order_list", [])
                st.success(f"✅ API OK — {len(_order_list)} order ditemukan (24 jam terakhir)")

                if _order_list:
                    for _o in _order_list:
                        _ct = _o.get("create_time", 0)
                        _ut = _o.get("update_time", 0)
                        _ct_str = _datetime.datetime.fromtimestamp(_ct).strftime("%Y-%m-%d %H:%M:%S") if _ct else "-"
                        _ut_str = _datetime.datetime.fromtimestamp(_ut).strftime("%Y-%m-%d %H:%M:%S") if _ut else "-"
                        st.json({
                            "order_sn":    _o.get("order_sn", "-"),
                            "order_status": _o.get("order_status", "-"),
                            "create_time": _ct_str,
                            "update_time": _ut_str,
                        })
                else:
                    st.info("Tidak ada order dalam 24 jam terakhir.")

            except ValueError as _e:
                st.error(f"❌ Parameter error: {_e}")
            except RuntimeError as _e:
                st.error(f"❌ Shopee API error: {_e}")
            except Exception as _e:
                st.error(f"❌ Unexpected error: {_e}")


        # TEMPORARY DIAGNOSTIC — get_order_detail raw GET
        # Sends request directly with requests.get() to inspect raw response.
        # Remove after diagnosis is complete.
        if st.button("🧪 Diagnostic: get_order_detail raw GET", key="btn_diag_order_detail"):
            import shopee_api as _shopee_api
            import shopee_auth as _shopee_auth_diag
            import time as _time
            import json as _json
            import requests as _requests

            _time_to   = int(_time.time())
            _time_from = _time_to - 86400

            # Step 1: get one order_sn — separate try block so its
            # exceptions cannot swallow the HTTP diagnostic output below.
            _test_sn = None
            try:
                _list_result = _shopee_api.get_order_list(
                    time_from=_time_from,
                    time_to=_time_to,
                    time_range_field="create_time",
                    page_size=1,
                )
                _list_orders = _list_result.get("order_list", [])
                if _list_orders:
                    _test_sn = _list_orders[0].get("order_sn", "")
                else:
                    st.info("Tidak ada order dalam 24 jam terakhir.")
            except Exception as _e:
                st.error(f"❌ get_order_list error: {_e}")

            # Step 2: raw diagnostic GET — only runs if we have an order_sn.
            if _test_sn:
                st.write(f"Diagnostic order_sn: `{_test_sn}`")
                try:
                    _access_token = _shopee_auth_diag.get_valid_access_token()
                    _tokens_diag  = _shopee_auth_diag.load_tokens()
                    _shop_id      = int(_tokens_diag.get("shop_id", 0))
                    _partner_id, _partner_key = _shopee_auth_diag.get_credentials()
                    _timestamp    = int(_time.time())

                    _sign = _shopee_api._generate_protected_signature(
                        _partner_id,
                        _shopee_api.ORDER_DETAIL_PATH,
                        _timestamp,
                        _access_token,
                        _shop_id,
                        _partner_key,
                    )

                    # order_sn_list sent as JSON array string per Shopee v2 spec.
                    _params = {
                        "partner_id":               _partner_id,
                        "timestamp":                _timestamp,
                        "sign":                     _sign,
                        "access_token":             _access_token,
                        "shop_id":                  _shop_id,
                        "order_sn_list":            _json.dumps([_test_sn]),
                        "response_optional_fields": "item_list",
                    }

                    _url = f"https://partner.shopeemobile.com{_shopee_api.ORDER_DETAIL_PATH}"

                    # Direct requests.get() — no raise_for_status(), so 4xx/5xx
                    # are displayed as-is rather than raised as exceptions.
                    _resp = _requests.get(
                        _url,
                        params=_params,
                        timeout=_shopee_api.REQUEST_TIMEOUT_SECONDS,
                    )

                    # Display status + raw body. URL is never shown because
                    # it contains access_token and signature.
                    st.write(f"**HTTP Status:** `{_resp.status_code}`")
                    try:
                        st.json(_resp.json())
                    except Exception:
                        st.code(_resp.text[:1000])

                except Exception as _e:
                    st.error(f"❌ Diagnostic request error: {_e}")
        # END TEMPORARY DIAGNOSTIC — get_order_detail raw GET

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


def save_packed_order(order_number):
    df = load_packed_df()
    order_number = str(order_number).strip()
    if order_number not in set(df["order_number"]):
        new_row = pd.DataFrame([{
            "order_number": order_number,
            "packed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }])
        df = pd.concat([df, new_row], ignore_index=True)
    df.to_csv(PACKED_FILE, index=False)


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


# ---- Session state for scan -> display -> confirm workflow ----
if "displayed_order" not in st.session_state:
    st.session_state.displayed_order = None
if "just_packed_order" not in st.session_state:
    st.session_state.just_packed_order = None
if "not_found_query" not in st.session_state:
    st.session_state.not_found_query = None


orders_df = load_orders()

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
                        save_packed_order(order_number)
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
                        save_packed_order(order_number)
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
                        st.write(f"**Kota:** {results.iloc[0].get('Kota/Kabupaten', '-')}")
                        st.write(f"**Provinsi:** {results.iloc[0].get('Provinsi', '-')}")
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
                        <td>{row.get('Provinsi', '-')}</td>
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
                        <tr><th>No. Pesanan</th><th>Username</th><th>Nama Penerima</th><th>Platform</th><th>Toko</th><th>Provinsi</th><th>Nama Logistik</th><th>Variasi</th><th>Qty</th></tr>
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

    st.divider()

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

    belum_df = unique_orders[unique_orders["__packable"] & ~unique_orders["__packed"]].copy()

    if belum_df.empty:
        st.success("Tidak ada order 'Perlu Dikirim' yang belum diverifikasi.")
    else:
        styled_belum_df = style_dashboard_table(
            belum_df.rename(columns={
                "No. Pesanan": "Order Number",
                "Username (Pembeli)": "Username",
                "Nama Penerima": "Recipient",
                "Platform": "Platform",
                "Toko": "Shop",
                "Provinsi": "Province",
                "Antar ke counter/ pick-up": "Shipping",
                "Nama Variasi": "Variant",
                "Jumlah": "Qty",
            })[["Order Number", "Username", "Recipient", "Platform", "Shop", "Province", "Shipping", "Variant", "Qty"]]
        )
        st.dataframe(
            styled_belum_df,
            use_container_width=True,
            hide_index=True,
        )

    # ---- Daily packing report (all orders packed today) ----
    st.divider()
    st.write("### 📅 Laporan Packing Hari Ini")

    packed_df = load_packed_df()
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_packed_df = packed_df[packed_df["packed_at"].astype(str).str.startswith(today_str)]

    if today_packed_df.empty:
        st.info("Belum ada order yang di-pack hari ini.")
    else:
        # Merge with order data to get product/buyer details, one row per order
        today_order_numbers = set(today_packed_df["order_number"])
        report_rows = orders_df[
            orders_df["No. Pesanan"].astype(str).str.strip().isin(today_order_numbers)
        ]

        st.write(f"**{len(today_order_numbers)} order** sudah di-pack hari ini ({today_str})")

        report_df = report_rows[
            ["No. Pesanan", "Username (Pembeli)", "Nama Penerima", "Platform", "Toko", "Provinsi", "Kota/Kabupaten", "Antar ke counter/ pick-up", "Nama Variasi", "Jumlah"]
        ].copy()

        styled_report_df = style_dashboard_table(
            report_df.rename(columns={
                "No. Pesanan": "Order Number",
                "Username (Pembeli)": "Username",
                "Nama Penerima": "Recipient",
                "Platform": "Platform",
                "Toko": "Shop",
                "Provinsi": "Province",
                "Kota/Kabupaten": "Kabupaten/Kota",
                "Antar ke counter/ pick-up": "Shipping",
                "Nama Variasi": "Variant",
                "Jumlah": "Qty"
            })
        )
        st.dataframe(
            styled_report_df,
            use_container_width=True,
            hide_index=True,
        )

        # Build printable daily report HTML
        report_table_rows = "".join(
            f"""
            <tr>
                <td>{r.get('No. Pesanan','-')}</td>
                <td>{r.get('Username (Pembeli)','-')}</td>
                <td>{r.get('Nama Penerima','-')}</td>
                <td>{r.get('Platform','-')}</td>
                <td>{r.get('Toko','-')}</td>
                <td>{r.get('Provinsi','-')}</td>
                <td>{r.get('Kota/Kabupaten','-')}</td>
                <td>{r.get('Antar ke counter/ pick-up','-')}</td>
                <td>{r.get('Nama Variasi','-')}</td>
                <td>{int(r.get('Jumlah',0)) if pd.notna(r.get('Jumlah')) else 0}</td>
            </tr>
            """
            for _, r in report_rows.iterrows()
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
                    <th>No. Pesanan</th><th>Username</th><th>Nama Penerima</th><th>Platform</th><th>Toko</th><th>Provinsi</th><th>Kabupaten/Kota</th><th>Nama Logistik</th><th>Variasi</th><th>Qty</th>
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