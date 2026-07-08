import streamlit as st
import pandas as pd
import os
import streamlit.components.v1 as components
from datetime import datetime

st.set_page_config(page_title="Shopee Packing Checker", layout="wide")
st.title("📦 Shopee Packing Checker")

DATA_FILE = "data/orders_master.csv"
PACKED_FILE = "packed.csv"

# Status values used by Shopee Indonesia exports that mean the order is cancelled
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


SNAPSHOT_FILE = "packed_snapshots.csv"
SNAPSHOT_RETENTION_DAYS = 7

SNAPSHOT_COLUMNS = [
    "order_number", "packed_at",
    "No. Pesanan", "Username (Pembeli)", "Nama Penerima", "Platform", "Toko",
    "Provinsi", "Kota/Kabupaten", "Antar ke counter/ pick-up",
    "Nama Variasi", "Jumlah",
]


def load_snapshot_df():
    if os.path.exists(SNAPSHOT_FILE):
        return pd.read_csv(SNAPSHOT_FILE)
    return pd.DataFrame(columns=SNAPSHOT_COLUMNS)


def save_packed_snapshot(order_number, order_rows, packed_at_str):
    """Store the product details for this order at the moment it's packed,
    so History can reprint it later even if orders_master.csv changes.
    Appends only — never modifies packed.csv. Auto-prunes snapshots older
    than SNAPSHOT_RETENTION_DAYS on every write to keep the file small."""
    df = load_snapshot_df()

    # Skip if this order already has a snapshot (avoid duplicates if packed twice)
    if not df.empty and order_number in set(df["order_number"].astype(str)):
        new_df = df
    else:
        new_rows = []
        for _, r in order_rows.iterrows():
            new_rows.append({
                "order_number": order_number,
                "packed_at": packed_at_str,
                "No. Pesanan": r.get("No. Pesanan", "-"),
                "Username (Pembeli)": r.get("Username (Pembeli)", "-"),
                "Nama Penerima": r.get("Nama Penerima", "-"),
                "Platform": r.get("Platform", "-"),
                "Toko": r.get("Toko", "-"),
                "Provinsi": r.get("Provinsi", "-"),
                "Kota/Kabupaten": r.get("Kota/Kabupaten", "-"),
                "Antar ke counter/ pick-up": r.get("Antar ke counter/ pick-up", "-"),
                "Nama Variasi": r.get("Nama Variasi", "-"),
                "Jumlah": r.get("Jumlah", 0),
            })
        new_df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)

    # Prune snapshots older than retention window
    new_df["__packed_at_dt"] = pd.to_datetime(new_df["packed_at"], errors="coerce")
    cutoff = pd.Timestamp(datetime.now()) - pd.Timedelta(days=SNAPSHOT_RETENTION_DAYS)
    new_df = new_df[new_df["__packed_at_dt"].isna() | (new_df["__packed_at_dt"] >= cutoff)]
    new_df = new_df.drop(columns=["__packed_at_dt"])

    new_df.to_csv(SNAPSHOT_FILE, index=False)


def save_packed_order(order_number, order_rows=None):
    df = load_packed_df()
    order_number = str(order_number).strip()
    if order_number not in set(df["order_number"]):
        packed_at_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_row = pd.DataFrame([{
            "order_number": order_number,
            "packed_at": packed_at_str,
        }])
        df = pd.concat([df, new_row], ignore_index=True)
        if order_rows is not None and not order_rows.empty:
            save_packed_snapshot(order_number, order_rows, packed_at_str)
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


def build_report_table_rows(report_rows):
    """Group report_rows by order number (rows assumed already adjacent per
    order) and render <tr> HTML with rowspan on order-level columns for
    multi-product orders. Returns the joined <tr>...</tr> HTML string.
    Used by both the on-screen table and the printable report, and by
    Packing History reprints, so all three stay visually consistent."""
    rows_list = list(report_rows.iterrows())
    row_groups = []
    for _, r in rows_list:
        order_no = r.get('No. Pesanan', '-')
        if row_groups and row_groups[-1][0] == order_no:
            row_groups[-1][1].append(r)
        else:
            row_groups.append((order_no, [r]))

    parts = []
    for order_no, rows in row_groups:
        n = len(rows)
        first = rows[0]
        rowspan_attr = f' rowspan="{n}"' if n > 1 else ""
        parts.append(f"""
        <tr>
            <td{rowspan_attr}>{first.get('No. Pesanan','-')}</td>
            <td{rowspan_attr}>{first.get('Username (Pembeli)','-')}</td>
            <td{rowspan_attr}>{first.get('Nama Penerima','-')}</td>
            <td{rowspan_attr}>{first.get('Platform','-')}</td>
            <td{rowspan_attr}>{first.get('Toko','-')}</td>
            <td{rowspan_attr}>{first.get('Provinsi','-')}</td>
            <td{rowspan_attr}>{first.get('Kota/Kabupaten','-')}</td>
            <td{rowspan_attr}>{first.get('Antar ke counter/ pick-up','-')}</td>
            <td>{first.get('Nama Variasi','-')}</td>
            <td>{int(first.get('Jumlah',0)) if pd.notna(first.get('Jumlah')) else 0}</td>
        </tr>
        """)
        for r in rows[1:]:
            parts.append(f"""
        <tr>
            <td>{r.get('Nama Variasi','-')}</td>
            <td>{int(r.get('Jumlah',0)) if pd.notna(r.get('Jumlah')) else 0}</td>
        </tr>
        """)
    return "".join(parts)


def render_screen_report_table(report_rows):
    """Render the rowspan-grouped report as an on-screen HTML table via st.markdown."""
    table_rows_html = build_report_table_rows(report_rows)
    table_html = f"""
    <style>
        .daily-report-table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
        .daily-report-table th {{ background:#f0f0f0; color:#1a1a1a; padding:10px; border:1px solid #444; text-align:center; vertical-align:middle; font-weight:bold; }}
        .daily-report-table td {{ padding:10px; border:1px solid #444; text-align:center; vertical-align:middle; }}
    </style>
    <table class="daily-report-table">
        <tr>
            <th>Order Number</th><th>Username</th><th>Recipient</th><th>Platform</th><th>Shop</th><th>Province</th><th>Kabupaten/Kota</th><th>Shipping</th><th>Variant</th><th>Qty</th>
        </tr>
        {table_rows_html}
    </table>
    """
    st.markdown(
        "\n".join(line.strip() for line in table_html.splitlines()),
        unsafe_allow_html=True,
    )


def build_printable_report_html(date_str, order_count, report_rows, summary_line=None):
    """Build the printable packing report HTML for a given date.
    summary_line: optional extra <p> HTML shown above the order-count line
    (used for today's live dashboard totals; omitted for historical
    reprints since those totals are 'right now', not 'as of that date')."""
    table_rows_html = build_report_table_rows(report_rows)
    summary_html = f'<p class="summary">{summary_line}</p>' if summary_line else ""
    return f"""
    <html>
    <head>
    <title>Laporan Packing {date_str}</title>
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
        <h1>📅 Laporan Packing Harian — {date_str}</h1>
        {summary_html}
        <p class="summary"><b>Di-pack: {order_count} order</b></p>
        <table>
            <tr>
                <th>No. Pesanan</th><th>Username</th><th>Nama Penerima</th><th>Platform</th><th>Toko</th><th>Provinsi</th><th>Kabupaten/Kota</th><th>Nama Logistik</th><th>Variasi</th><th>Qty</th>
            </tr>
            {table_rows_html}
        </table>
        <p style="margin-top:24px;font-size:12px;color:#888;">Dicetak: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    </body>
    </html>
    """


def print_report_button(button_label, button_key, daily_report_html):
    """Render a print button that opens the given report HTML in a new window."""
    if st.button(button_label, key=button_key):
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

        # On-screen table: same rowspan grouping as the printable report,
        # so multi-product orders look identical on-screen and on print.
        render_screen_report_table(report_rows)

        today_summary_line = (
            f"Total Order: {total_orders} &nbsp;|&nbsp; "
            f"Sudah Di-Pack: {total_packed} &nbsp;|&nbsp; "
            f"Perlu Dikirim: {packable_orders} &nbsp;|&nbsp; "
            f"Batal: {total_cancelled}"
        )
        daily_report_html = build_printable_report_html(
            today_str, len(today_order_numbers), report_rows, summary_line=today_summary_line
        )
        print_report_button("🖨️ Print Laporan Hari Ini", "btn_print_today", daily_report_html)

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

        # Daily breakdown — counts unique orders packed per day (not product
        # rows), consistent with how "Laporan Packing Hari Ini" counts orders.
        st.subheader("Daily Breakdown")

        packed_df_valid["__date"] = packed_df_valid["packed_at"].dt.date
        daily_counts = (
            packed_df_valid.drop_duplicates(subset=["order_number", "__date"])
            .groupby("__date").size().reset_index()
        )
        daily_counts.columns = ["Date", "Total Orders"]
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
        st.line_chart(chart_data.set_index("Date")["Total Orders"], use_container_width=True)

        # ---- View / reprint a previous packing session ----
        st.subheader("Lihat / Print Sesi Sebelumnya")

        available_dates = [str(d) for d in daily_counts["Date"]]
        selected_date_str = st.selectbox(
            "Pilih tanggal packing", available_dates, key="history_session_date"
        )

        if selected_date_str:
            session_packed_df = packed_df_valid[packed_df_valid["__date"].astype(str) == selected_date_str]
            session_order_numbers_ordered = list(session_packed_df["order_number"])  # preserve pack order

            snapshot_df = load_snapshot_df()
            snapshot_orders_here = set(
                snapshot_df["order_number"].astype(str)
            ) if not snapshot_df.empty else set()

            session_report_parts = []
            missing_from_snapshot = []
            for order_no in session_order_numbers_ordered:
                if order_no in snapshot_orders_here:
                    rows = snapshot_df[snapshot_df["order_number"].astype(str) == order_no]
                    session_report_parts.append(rows)
                else:
                    rows = orders_df[orders_df["No. Pesanan"].astype(str).str.strip() == order_no]
                    if rows.empty:
                        missing_from_snapshot.append(order_no)
                    else:
                        session_report_parts.append(rows)

            session_report_rows = (
                pd.concat(session_report_parts, ignore_index=True)
                if session_report_parts else pd.DataFrame()
            )

            st.write(f"**{len(session_order_numbers_ordered)} order** di-pack pada {selected_date_str}")

            if missing_from_snapshot:
                st.caption(
                    f"⚠️ {len(missing_from_snapshot)} order tidak ditemukan (tidak ada snapshot "
                    f"dan sudah tidak ada di data/orders_master.csv): {', '.join(missing_from_snapshot)}"
                )

            if session_report_rows.empty:
                st.warning(
                    "Tidak ada detail produk yang tersisa untuk sesi ini "
                    "(snapshot sudah lewat masa simpan 7 hari, dan order sudah tidak ada di export terbaru)."
                )
            else:
                render_screen_report_table(session_report_rows)

                session_report_html = build_printable_report_html(
                    selected_date_str, len(session_order_numbers_ordered), session_report_rows
                )
                print_report_button(
                    "🖨️ Print Laporan Sesi Ini",
                    f"btn_print_session_{selected_date_str}",
                    session_report_html,
                )