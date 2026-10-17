"""
One-time, idempotent migration: packed.csv / packed_snapshots.csv -> Supabase.

Target schema (already created in Supabase by hand; this script does NOT
create, alter, or drop any table):

    packed_orders
        shop_id      bigint
        order_number text
        packed_at    timestamptz

    packed_snapshot_items
        id              (identity / default, not set by this script)
        shop_id         bigint
        order_number    text
        packed_at       timestamptz
        no_pesanan      text
        username        text
        nama_penerima   text
        platform        text
        toko            text
        provinsi        text
        kota_kabupaten  text
        shipping        text
        ekspedisi       text
        nama_variasi    text
        jumlah          integer

Multi-variant orders produce multiple packed_snapshot_items rows (one per
CSV row for that order). To make repeated runs idempotent via upsert, each
row needs a stable synthetic position within its (shop_id, order_number)
group. If not already present, run this once against the existing table:

    ALTER TABLE packed_snapshot_items ADD COLUMN IF NOT EXISTS row_index integer;
    ALTER TABLE packed_snapshot_items
        ADD CONSTRAINT packed_snapshot_items_shop_order_row_key
        UNIQUE (shop_id, order_number, row_index);

shop_id handling (IMPORTANT — no guessing):
    This script reads shop_id directly from the shop_id column already
    present in packed.csv / packed_snapshots.csv (both files carry it as
    part of the app's V2.1 multi-shop migration). It is NEVER inferred,
    defaulted, or backfilled to any particular shop. A row whose shop_id
    is blank, non-numeric, or not a value in KNOWN_SHOP_IDS is collected
    as an "unresolved shop mapping" and BLOCKS the migration entirely —
    no rows are written to Supabase (even in --execute mode) while any
    unresolved mapping exists.

Blank packed.csv rows (no order_number after stripping) are skipped and
counted/reported separately from unresolved-shop rows; they are not an
error condition.

Usage:
    python3 migrate_packing_history.py              # dry run (default)
    python3 migrate_packing_history.py --dry-run     # explicit dry run
    python3 migrate_packing_history.py --execute     # actually write to Supabase

Env / secrets:
    Reads SUPABASE_URL / SUPABASE_KEY from the environment first. If either
    is unset, falls back to reading them from .streamlit/secrets.toml (same
    file app.py's _inject_supabase_secrets() reads from), WITHOUT overwriting
    any already-set env var. Values are never printed or logged.
"""

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

PACKED_FILE = "packed.csv"
SNAPSHOT_FILE = "packed_snapshots.csv"

PACKED_ORDERS_TABLE = "packed_orders"
SNAPSHOT_ITEMS_TABLE = "packed_snapshot_items"

# Known shop_id values this app is allowed to migrate. Mirrors app.py's
# SHOPEE_SHOPS registry. A row whose shop_id isn't in here is unresolved,
# not guessed into one of these.
KNOWN_SHOP_IDS = {
    1272241861: "Onbie",
    1179306776: "Gotto",
}

# One-time, explicit legacy mapping for this migration only.
# packed.csv / packed_snapshots.csv predate the shop_id column entirely
# (V2.1 added it) — these specific order numbers were manually confirmed
# against external sources (explicit Toko data, a June 2026 Shopee export)
# and provided directly by the operator. This is ONLY consulted as a
# fallback when a row has no shop_id column value at all (see
# _resolve_shop_id below); it never overrides or is consulted for a row
# that already carries its own shop_id, so it has zero effect on current
# or future V2.1+ data.
LEGACY_ORDER_SHOP_MAP = {
    # ONBIE — shop_id 1272241861
    "260502AE5B5692": 1272241861,
    "2606289Q94AYMU": 1272241861,
    "260622PJE9EHVK": 1272241861,
    "260622QNR7231P": 1272241861,
    "260624W0SKXR7T": 1272241861,
    "2606251JNHM4JB": 1272241861,
    "2606251QCD59EF": 1272241861,
    "2606251QSDXH5P": 1272241861,
    "2606251U8YFV2Q": 1272241861,
    # GOTTO — shop_id 1179306776
    "260620JTA4UGA1": 1179306776,
    "260620JQMA8H94": 1179306776,
    "260621MW8PEGPT": 1179306776,
    "260621NP6RW6X9": 1179306776,
    "260621P0NHGH2E": 1179306776,
}

# packed_snapshots.csv column -> packed_snapshot_items column
SNAPSHOT_COLUMN_MAP = {
    "No. Pesanan": "no_pesanan",
    "Username (Pembeli)": "username",
    "Nama Penerima": "nama_penerima",
    "Platform": "platform",
    "Toko": "toko",
    "Provinsi": "provinsi",
    "Kota/Kabupaten": "kota_kabupaten",
    "Antar ke counter/ pick-up": "shipping",
    "Ekspedisi": "ekspedisi",
    "Nama Variasi": "nama_variasi",
    "Jumlah": "jumlah",
}

WIB = ZoneInfo("Asia/Jakarta")

BATCH_SIZE = 500


class MigrationBlocked(Exception):
    """Raised when validation finds unresolved shop mappings — migration
    must not proceed (dry-run reporting still happens; no Supabase write)."""


# ---------------------------------------------------------------------
# Env / secrets — unchanged pattern: env vars win, .streamlit/secrets.toml
# is a fallback only, never overwrites an already-set env var, never
# prints values.
# ---------------------------------------------------------------------
def _load_env_from_streamlit_secrets():
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        return  # env already fully set — nothing to do

    secrets_path = os.path.join(".streamlit", "secrets.toml")
    if not os.path.exists(secrets_path):
        return

    try:
        try:
            import tomllib  # Python 3.11+
            with open(secrets_path, "rb") as f:
                data = tomllib.load(f)
        except ImportError:
            import toml  # fallback for older Python
            with open(secrets_path, "r") as f:
                data = toml.load(f)
    except Exception as e:
        print(f"[warn] could not read {secrets_path}: {type(e).__name__}")
        return

    if "SUPABASE_URL" not in os.environ and data.get("SUPABASE_URL"):
        os.environ["SUPABASE_URL"] = str(data["SUPABASE_URL"]).strip()
    if "SUPABASE_KEY" not in os.environ and data.get("SUPABASE_KEY"):
        os.environ["SUPABASE_KEY"] = str(data["SUPABASE_KEY"]).strip()


def _get_supabase_client():
    """Reuses the same connection setup pattern as shopee_auth.py:
    lazy client from SUPABASE_URL/SUPABASE_KEY env vars."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_KEY not set (checked env and .streamlit/secrets.toml)."
        )
    from supabase import create_client
    return create_client(url, key)


# ---------------------------------------------------------------------
# Timestamp handling
# ---------------------------------------------------------------------
def _to_timestamptz(value):
    """Naive WIB string -> ISO 8601 string with +07:00 offset, or None
    (never an empty string) for blank/unparseable values."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = pd.to_datetime(s).to_pydatetime()
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=WIB)
    return dt.isoformat()


# ---------------------------------------------------------------------
# shop_id validation — never guessed
# ---------------------------------------------------------------------
def _resolve_shop_id(raw_value, order_number=None):
    """Returns (shop_id:int, None, via:str, False) if valid/known, or
    (None, reason:str, None, blocking:bool) if the row can't be resolved.

    blocking distinguishes two different failure modes:
      - True:  the row HAS a shop_id column value, but it's non-numeric or
               not in KNOWN_SHOP_IDS. This is current/V2.1+-schema data and
               stays strict — it blocks the migration like before.
      - False: the row has NO shop_id column value at all, and its
               order_number isn't in LEGACY_ORDER_SHOP_MAP either. This is
               a pre-V2.1 row with nothing to resolve it — it's skipped,
               not fatal (see LEGACY_ORDER_SHOP_MAP comment: never guessed).

    Resolution order:
      1. The row's own shop_id column value, if present (unchanged
         behavior — this is the only path for V2.1+ data).
      2. Only when that's absent/blank: LEGACY_ORDER_SHOP_MAP, by exact
         order_number match. Never consulted otherwise."""
    s = "" if raw_value is None else str(raw_value).strip()
    if s and s.lower() != "nan":
        try:
            shop_id = int(float(s))
        except ValueError:
            return None, f"non-numeric shop_id {s!r}", None, True
        if shop_id not in KNOWN_SHOP_IDS:
            return None, f"unknown shop_id {shop_id} (not in KNOWN_SHOP_IDS)", None, True
        return shop_id, None, "shop_id_column", False

    if order_number is not None and order_number in LEGACY_ORDER_SHOP_MAP:
        return LEGACY_ORDER_SHOP_MAP[order_number], None, "legacy_map", False

    return (
        None,
        "missing shop_id (no shop_id column value, not in LEGACY_ORDER_SHOP_MAP) — skipped",
        None,
        False,
    )


# ---------------------------------------------------------------------
# packed.csv -> packed_orders rows
# ---------------------------------------------------------------------
def prepare_packed_orders(df):
    """Returns (rows, report) where rows is the list of dicts ready for
    packed_orders, and report has counts + unresolved-row detail."""
    rows = []
    blank_count = 0
    unresolved = []
    skipped_legacy = []
    legacy_mapped = 0
    seen_keys = {}
    duplicates = []

    for idx, r in df.iterrows():
        order_number = str(r.get("order_number", "")).strip()
        if not order_number or order_number.lower() == "nan":
            blank_count += 1
            continue

        shop_id, reason, via, blocking = _resolve_shop_id(r.get("shop_id"), order_number)
        if reason is not None:
            entry = {
                "source": "packed.csv",
                "row": int(idx),
                "order_number": order_number,
                "reason": reason,
            }
            (unresolved if blocking else skipped_legacy).append(entry)
            continue
        if via == "legacy_map":
            legacy_mapped += 1

        key = (shop_id, order_number)
        if key in seen_keys:
            duplicates.append({
                "source": "packed.csv",
                "row": int(idx),
                "order_number": order_number,
                "shop_id": shop_id,
                "conflicts_with_row": seen_keys[key],
            })
            continue
        seen_keys[key] = int(idx)

        rows.append({
            "shop_id": shop_id,
            "order_number": order_number,
            "packed_at": _to_timestamptz(r.get("packed_at")),
        })

    report = {
        "total_rows": len(df),
        "blank_rows_skipped": blank_count,
        "unresolved": unresolved,
        "skipped_legacy": skipped_legacy,
        "legacy_mapped": legacy_mapped,
        "duplicates": duplicates,
        "prepared_rows": len(rows),
    }
    return rows, report


# ---------------------------------------------------------------------
# packed_snapshots.csv -> packed_snapshot_items rows
# ---------------------------------------------------------------------
def prepare_snapshot_items(df):
    rows = []
    blank_count = 0
    unresolved = []
    skipped_legacy = []
    legacy_mapped = 0
    duplicates = []

    # row_index = 0-based position within each (shop_id, order_number)
    # group, in file order — needed for idempotent upsert on multi-variant
    # orders. Computed per raw shop_id value so an unresolved row doesn't
    # shift the index of valid rows in the same order.
    group_counters = {}
    # row_index is monotonically assigned per (shop_id, order_number), so
    # item_key collisions are structurally impossible here — this stays
    # purely as an explicit, visible guard (and keeps the report shape
    # symmetric with packed_orders) rather than a silent assumption.
    seen_item_keys = {}

    for idx, r in df.iterrows():
        order_number = str(r.get("order_number", "")).strip()
        if not order_number or order_number.lower() == "nan":
            blank_count += 1
            continue

        shop_id, reason, via, blocking = _resolve_shop_id(r.get("shop_id"), order_number)
        if reason is not None:
            entry = {
                "source": "packed_snapshots.csv",
                "row": int(idx),
                "order_number": order_number,
                "reason": reason,
            }
            (unresolved if blocking else skipped_legacy).append(entry)
            continue
        if via == "legacy_map":
            legacy_mapped += 1

        key = (shop_id, order_number)
        row_index = group_counters.get(key, 0)
        group_counters[key] = row_index + 1

        item_key = (shop_id, order_number, row_index)
        if item_key in seen_item_keys:
            duplicates.append({
                "source": "packed_snapshots.csv",
                "row": int(idx),
                "order_number": order_number,
                "shop_id": shop_id,
                "row_index": row_index,
                "conflicts_with_row": seen_item_keys[item_key],
            })
            continue
        seen_item_keys[item_key] = int(idx)

        item = {
            "shop_id": shop_id,
            "order_number": order_number,
            "packed_at": _to_timestamptz(r.get("packed_at")),
            "row_index": row_index,
        }
        for csv_col, db_col in SNAPSHOT_COLUMN_MAP.items():
            val = r.get(csv_col)
            if db_col == "jumlah":
                try:
                    item[db_col] = int(float(val)) if pd.notna(val) else None
                except (ValueError, TypeError):
                    item[db_col] = None
            else:
                item[db_col] = None if pd.isna(val) else str(val)
        rows.append(item)

    report = {
        "total_rows": len(df),
        "blank_rows_skipped": blank_count,
        "unresolved": unresolved,
        "skipped_legacy": skipped_legacy,
        "legacy_mapped": legacy_mapped,
        "duplicates": duplicates,
        "prepared_rows": len(rows),
    }
    return rows, report


# ---------------------------------------------------------------------
# Supabase write (only reached in --execute mode, and only if no
# unresolved mappings exist anywhere)
# ---------------------------------------------------------------------
def _upsert_batches(client, table, rows, on_conflict):
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        client.table(table).upsert(batch, on_conflict=on_conflict).execute()


def migrate_packed_orders(client, rows, execute):
    if execute:
        _upsert_batches(client, PACKED_ORDERS_TABLE, rows, on_conflict="shop_id,order_number")


def migrate_snapshot_items(client, rows, execute):
    if execute:
        _upsert_batches(client, SNAPSHOT_ITEMS_TABLE, rows, on_conflict="shop_id,order_number,row_index")


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def _print_report(label, report):
    print(f"\n--- {label} ---")
    print(f"  total rows in file:      {report['total_rows']}")
    print(f"  blank rows skipped:      {report['blank_rows_skipped']}")
    print(f"  resolved via legacy map: {report['legacy_mapped']}")
    print(f"  prepared for migration:  {report['prepared_rows']}")
    print(f"  unresolved shop rows (BLOCKING): {len(report['unresolved'])}")
    for u in report["unresolved"][:20]:
        print(f"    [{u['source']}] row {u['row']} order_number={u['order_number']!r}: {u['reason']}")
    if len(report["unresolved"]) > 20:
        print(f"    ... and {len(report['unresolved']) - 20} more")
    print(f"  unresolved/skipped legacy orders (not written, not fatal): {len(report['skipped_legacy'])}")
    for u in report["skipped_legacy"][:20]:
        print(f"    [{u['source']}] row {u['row']} order_number={u['order_number']!r}: {u['reason']}")
    if len(report["skipped_legacy"]) > 20:
        print(f"    ... and {len(report['skipped_legacy']) - 20} more")
    print(f"  duplicate conflicts:     {len(report['duplicates'])}")
    for d in report["duplicates"][:20]:
        print(f"    [{d['source']}] row {d['row']} order_number={d['order_number']!r} "
              f"conflicts with row {d['conflicts_with_row']}")
    if len(report["duplicates"]) > 20:
        print(f"    ... and {len(report['duplicates']) - 20} more")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                         help="Actually write to Supabase. Without this flag, runs as a dry run.")
    args = parser.parse_args()
    execute = args.execute

    if not os.path.exists(PACKED_FILE):
        print(f"❌ {PACKED_FILE} not found in current directory.")
        sys.exit(1)
    if not os.path.exists(SNAPSHOT_FILE):
        print(f"❌ {SNAPSHOT_FILE} not found in current directory.")
        sys.exit(1)

    packed_df = pd.read_csv(PACKED_FILE, dtype=str)
    snapshot_df = pd.read_csv(SNAPSHOT_FILE, dtype=str)

    packed_rows, packed_report = prepare_packed_orders(packed_df)
    snapshot_rows, snapshot_report = prepare_snapshot_items(snapshot_df)

    _print_report("packed.csv -> packed_orders", packed_report)
    _print_report("packed_snapshots.csv -> packed_snapshot_items", snapshot_report)

    total_unresolved = len(packed_report["unresolved"]) + len(snapshot_report["unresolved"])
    total_skipped_legacy = len(packed_report["skipped_legacy"]) + len(snapshot_report["skipped_legacy"])

    print(f"\n{'DRY RUN' if not execute else 'EXECUTE'} mode.")

    if total_skipped_legacy > 0:
        print(
            f"\n⚠️ {total_skipped_legacy} row(s) are unresolved/skipped legacy orders "
            "(no shop_id column, not in LEGACY_ORDER_SHOP_MAP). They will NOT produce "
            "packed_orders or packed_snapshot_items rows and are not guessed — see the "
            "'unresolved/skipped legacy orders' lines above."
        )

    if total_unresolved > 0:
        print(
            f"\n❌ BLOCKED: {total_unresolved} row(s) have a shop_id value that's invalid "
            "or not in KNOWN_SHOP_IDS. Migration will not write anything until every "
            "current-schema row resolves to a known shop_id."
        )
        if execute:
            sys.exit(1)
        else:
            print("(dry run — reporting only; this would also block a real --execute run)")
            return

    print(f"\n✅ CLEAR. Ready to migrate "
          f"{packed_report['prepared_rows']} packed_orders row(s) and "
          f"{snapshot_report['prepared_rows']} packed_snapshot_items row(s) "
          f"({total_skipped_legacy} legacy row(s) skipped, not migrated).")

    if not execute:
        print("Dry run only — no Supabase connection made, nothing written.")
        return

    _load_env_from_streamlit_secrets()
    client = _get_supabase_client()

    migrate_packed_orders(client, packed_rows, execute=True)
    migrate_snapshot_items(client, snapshot_rows, execute=True)

    # Post-write verification: compare prepared count to what's actually
    # in Supabase now.
    for table, expected, label in (
        (PACKED_ORDERS_TABLE, len(packed_rows), "packed_orders"),
        (SNAPSHOT_ITEMS_TABLE, len(snapshot_rows), "packed_snapshot_items"),
    ):
        try:
            resp = client.table(table).select("shop_id", count="exact", head=True).execute()
            actual = resp.count
            status = "✅" if actual is not None and actual >= expected else "⚠️"
            print(f"{status} {label}: prepared {expected}, Supabase now reports {actual}")
        except Exception as e:
            print(f"⚠️ {label}: could not verify count ({type(e).__name__})")


if __name__ == "__main__":
    main()
