#!/usr/bin/env python3
"""
convert_sources_to_master.py
=============================

Merges whichever of these exist into ONE standardized data/orders_master.csv:
    data/eb.xlsx   -> EasyBoss export
    data/spm.xlsx  -> Shopee Partmachine export
    data/tpm.xlsx  -> TikTok Partmachine export

app.py only ever reads data/orders_master.csv. It does not know or care
how many sources were merged into it.

⚠️ IMPORTANT — READ BEFORE FIRST REAL RUN
-------------------------------------------
- The Shopee Partmachine adapter (adapt_shopee_partmachine) uses the
  SAME column names as your existing Onbie Shopee export, so it's
  ready to use right now.
- The EasyBoss and TikTok Partmachine column mappings below
  (EASYBOSS_COLUMN_MAP / TIKTOK_COLUMN_MAP) are PLACEHOLDERS. I don't
  have a real export sample from those two yet, so the column names on
  the right-hand side are guesses. Run this once with a real eb.xlsx
  or tpm.xlsx — if columns are missing, the script will print the
  ACTUAL column names found in your file so you can fix the map in
  2 minutes.

HOW TO RUN
----------
    python3 convert_sources_to_master.py

Normally you don't run this directly — import_watcher.py calls it
automatically after copying a freshly-downloaded file into place.
"""

import os
import sys
import json
import tempfile
from datetime import datetime

import pandas as pd

DATA_DIR = "data"
OUTPUT_FILE = os.path.join(DATA_DIR, "orders_master.csv")
SYNC_META_FILE = os.path.join(DATA_DIR, "last_sync.json")

STANDARD_COLUMNS = [
    "No. Pesanan",
    "No. Resi",
    "Username (Pembeli)",
    "Nama Penerima",
    "Kota/Kabupaten",
    "Provinsi",
    "SKU Induk",
    "Nama Produk",
    "Nama Barang",
    "Nama Variasi",
    "Jumlah",
    "Berat (Kg)",
    "Status Pesanan",
    "Waktu Pesanan Dibuat",
    "Tenggat Pengiriman",
    "Antar ke counter/ pick-up",
    "Catatan dari Pembeli",
    "Platform",
    "Toko",
    "Sumber",
]


def _ensure_standard_columns(df):
    """Ensure all STANDARD_COLUMNS exist in dataframe, filling missing with '-'."""
    for col in STANDARD_COLUMNS:
        if col not in df.columns:
            df[col] = "-"
    return df


# --------- Shopee Partmachine adapter (known-good format) ---------

def adapt_shopee_partmachine(df):
    """Known-good: same export format as the existing Onbie Shopee data."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    out = pd.DataFrame()
    out["No. Pesanan"] = df.get("No. Pesanan", "-")
    out["No. Resi"] = df.get("No. Resi", "-")
    out["Username (Pembeli)"] = df.get("Username (Pembeli)", "-")
    out["Nama Penerima"] = df.get("Nama Penerima", "-")
    out["Kota/Kabupaten"] = df.get("Kota/Kabupaten", "-")
    out["Provinsi"] = df.get("Provinsi", "-")
    out["SKU Induk"] = df.get("SKU Induk", "-")
    out["Nama Produk"] = df.get("Nama Produk", "-")
    out["Nama Barang"] = df.get("Nama Produk", "-")
    out["Nama Variasi"] = df.get("Nama Variasi", "-")
    out["Jumlah"] = pd.to_numeric(df.get("Jumlah", 0), errors="coerce").fillna(0)
    out["Berat (Kg)"] = "-"
    out["Status Pesanan"] = df.get("Status Pesanan", "-")
    out["Waktu Pesanan Dibuat"] = df.get("Waktu Pesanan Dibuat", "-")
    out["Tenggat Pengiriman"] = "-"
    out["Antar ke counter/ pick-up"] = df.get("Antar ke counter/ pick-up", "-")
    out["Catatan dari Pembeli"] = df.get("Catatan dari Pembeli", "-")
    out["Platform"] = "Shopee"
    out["Toko"] = "Partmachine"
    out["Sumber"] = "Shopee Partmachine"
    return out


# --------- EasyBoss adapter (confirmed real export format) ---------
#
# Confirmed columns from EasyBoss export:
#   Platform, Nama Toko, No. Pesanan, Nomor Resi, Status Pesanan,
#   Nama Logistik, Pesan Pembeli, Waktu Pemesanan,
#   Tenggat waktu pengiriman barang, Judul, Spesifikasi Produk,
#   Kuantitas Produk, Nama Barang, SKU Barang, Berat Barang (Kg),
#   Nama, Provinsi, Kota
#
# Note: EasyBoss does NOT have "Username (Pembeli)" — that column is filled with "-"
#
# Columns intentionally dropped (not in export):
#   Metode Pembayaran, Total Pesanan Produk

EASYBOSS_COLUMN_MAP = {
    "Platform": "Platform",
    "Toko": "Nama Toko",
    "No. Pesanan": "No. Pesanan",
    "No. Resi": "Nomor Resi",
    "Status Pesanan": "Status Pesanan",
    "Antar ke counter/ pick-up": "Nama Logistik",
    "Catatan dari Pembeli": "Pesan Pembeli",
    "Waktu Pesanan Dibuat": "Waktu Pemesanan",
    "Tenggat Pengiriman": "Tenggat waktu pengiriman barang",
    "Nama Produk": "Judul",
    "Nama Variasi": "Spesifikasi Produk",
    "Jumlah": "Kuantitas Produk",
    "Nama Barang": "Nama Barang",
    "SKU Induk": "SKU Barang",
    "Berat (Kg)": "Berat Barang (Kg)",
    "Nama Penerima": "Nama",
    "Provinsi": "Provinsi",
    "Kota/Kabupaten": "Kota",
    "Username (Pembeli)": "Nama Panggilan Pembeli",
}

EASYBOSS_STATUS_MAP = {
    "menunggu pengiriman": "Perlu Dikirim",
    "dikirim": "Sedang Dikirim",
    "selesai": "Selesai",
    "batal": "Batal",
    "cancelled": "Batal",
    "returned": "Retur",
}


def translate_easyboss_status(raw_status):
    key = str(raw_status).strip().lower()
    return EASYBOSS_STATUS_MAP.get(key, raw_status)


def adapt_easyboss(df):
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    out = pd.DataFrame()
    missing = []

    for std_col, raw_col in EASYBOSS_COLUMN_MAP.items():
        if raw_col in df.columns:
            out[std_col] = df[raw_col]
        else:
            missing.append(raw_col)
            out[std_col] = "-"

    if missing:
        print(f"⚠️  EasyBoss: kolom berikut TIDAK ditemukan (diisi '-'): {missing}")
        print(f"    Kolom asli yang ada di file ini: {list(df.columns)}")
        print(f"    -> Edit EASYBOSS_COLUMN_MAP di convert_sources_to_master.py lalu run ulang.")

    # EasyBoss exports merged cells for order-level info when an order has
    # multiple products: only the FIRST product row has order number, buyer,
    # address, etc. — subsequent product rows for the same order are blank
    # in those columns. Forward-fill them so every product row keeps its
    # parent order's identity (otherwise extra product lines become
    # invisible when scanning/searching by No. Pesanan).
    order_level_cols = [
        "Platform", "Toko", "No. Pesanan", "No. Resi", "Status Pesanan",
        "Antar ke counter/ pick-up", "Catatan dari Pembeli",
        "Waktu Pesanan Dibuat", "Tenggat Pengiriman",
        "Nama Penerima", "Provinsi", "Kota/Kabupaten", "Username (Pembeli)",
    ]
    out[order_level_cols] = out[order_level_cols].ffill()

    out["Jumlah"] = pd.to_numeric(out.get("Jumlah", 0), errors="coerce").fillna(0)
    out["Berat (Kg)"] = pd.to_numeric(out.get("Berat (Kg)", "-"), errors="coerce")
    out["Berat (Kg)"] = out["Berat (Kg)"].fillna("-")
    out["Status Pesanan"] = out["Status Pesanan"].apply(translate_easyboss_status)
    out["Sumber"] = "EasyBoss"

    return _ensure_standard_columns(out)


# --------- TikTok Partmachine adapter (placeholder) ---------
#
# ⚠️ PLACEHOLDER — confirm against a real TikTok Partmachine export and
# fix the right-hand side column names below.

TIKTOK_COLUMN_MAP = {
    "No. Pesanan": "Order ID",
    "No. Resi": "Tracking ID",
    "Username (Pembeli)": "Buyer Username",
    "Nama Penerima": "Recipient",
    "Kota/Kabupaten": "City",
    "Provinsi": "Province",
    "SKU Induk": "Seller SKU",
    "Nama Produk": "Product Name",
    "Nama Variasi": "Variation",
    "Jumlah": "Quantity",
    "Status Pesanan": "Order Status",
    "Waktu Pesanan Dibuat": "Created Time",
}


def _adapt_via_column_map(df, column_map, source_label):
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    out = pd.DataFrame()
    missing = []

    for std_col, raw_col in column_map.items():
        if raw_col in df.columns:
            out[std_col] = df[raw_col]
        else:
            missing.append(raw_col)
            out[std_col] = "-"

    if missing:
        print(f"⚠️  {source_label}: kolom berikut TIDAK ditemukan (diisi '-'): {missing}")
        print(f"    Kolom asli yang ada di file ini: {list(df.columns)}")
        print(f"    -> Edit {source_label.upper()}_COLUMN_MAP di convert_sources_to_master.py lalu run ulang.")

    out["Jumlah"] = pd.to_numeric(out.get("Jumlah", 0), errors="coerce").fillna(0)
    out["Antar ke counter/ pick-up"] = "-"
    out["Catatan dari Pembeli"] = "-"
    out["Platform"] = source_label.split()[0]
    out["Toko"] = " ".join(source_label.split()[1:]) if len(source_label.split()) > 1 else source_label
    out["Sumber"] = source_label

    return _ensure_standard_columns(out)


def adapt_tiktok_partmachine(df):
    return _adapt_via_column_map(df, TIKTOK_COLUMN_MAP, "TikTok Partmachine")


# (filename in data/, label, adapter function)
SOURCES = [
    ("eb.xlsx", "EasyBoss", adapt_easyboss),
    ("spm.xlsx", "Shopee Partmachine", adapt_shopee_partmachine),
    ("tpm.xlsx", "TikTok Partmachine", adapt_tiktok_partmachine),
]


def write_sync_meta(success, order_count=None, error=None, sources_used=None):
    os.makedirs(DATA_DIR, exist_ok=True)
    meta = {
        "synced_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "success": success,
        "order_count": order_count,
        "error": str(error) if error else None,
        "source": "multi_source_excel",
        "sources_used": sources_used or [],
    }
    with open(SYNC_META_FILE, "w") as f:
        json.dump(meta, f, indent=2)


def main():
    print("📦 Converting multi-source exports -> orders_master.csv\n" + "-" * 50)
    frames = []
    sources_used = []

    for filename, label, adapter in SOURCES:
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            print(f"⏭️  {label}: {path} tidak ada, dilewati.")
            continue
        try:
            raw = pd.read_excel(path)
            mapped = adapter(raw)
            frames.append(mapped)
            sources_used.append(label)
            print(f"✅ {label}: {len(mapped)} baris dibaca dari {path}")
        except Exception as e:
            print(f"❌ {label}: gagal dibaca ({e}) — dilewati, sumber lain tetap diproses.")

    if not frames:
        msg = "Tidak ada file sumber (eb.xlsx / spm.xlsx / tpm.xlsx) ditemukan di data/."
        print(f"❌ {msg}")
        write_sync_meta(success=False, error=msg)
        sys.exit(1)

    combined = pd.concat(frames, ignore_index=True)[STANDARD_COLUMNS]

    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".csv.tmp")
    os.close(fd)
    combined.to_csv(tmp_path, index=False)
    os.replace(tmp_path, OUTPUT_FILE)

    unique_orders = combined["No. Pesanan"].astype(str).str.strip().nunique()
    write_sync_meta(success=True, order_count=unique_orders, sources_used=sources_used)

    print(
        f"\n✅ Selesai. {unique_orders} order unik ({len(combined)} baris produk) "
        f"dari {len(sources_used)} sumber: {', '.join(sources_used)}"
    )
    print(f"   Ditulis ke {OUTPUT_FILE}")


if __name__ == "__main__":
    main()