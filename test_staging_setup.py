"""
Tests for supabase_staging_setup.sql on a THROWAWAY local PostgreSQL (temp
dir, unix socket, no network). Never touches Supabase. Supabase's roles /
auth schema are stubbed (see test_packer_sql.BOOTSTRAP). Skipped if
PostgreSQL binaries or psycopg2 are missing.

    python3 test_staging_setup.py

Checks: PART 2 is byte-identical to supabase_auth_setup.sql; the script runs
on a fresh database and twice in a row; every table/column/type/unique
constraint the Python code relies on exists (derived from app.py /
shopee_auth.py by AST, not hard-coded twice); RLS + privileges (anon /
authenticated get nothing, service_role only what is granted); PostgREST-
style upserts succeed as service_role; the PART 0 guard aborts on a
database that already holds data.
"""
import ast
import os
import re
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
import test_packer_sql as tps  # noqa: E402

STAGING = open(os.path.join(REPO, "supabase_staging_setup.sql")).read()
AUTH = open(os.path.join(REPO, "supabase_auth_setup.sql")).read()
APP_SRC = open(os.path.join(REPO, "app.py")).read()
SA_SRC = open(os.path.join(REPO, "shopee_auth.py")).read()
psycopg2 = tps.psycopg2
q = tps.q
expect_error = tps.expect_error


def _app_const(name):
    for n in ast.parse(APP_SRC).body:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == name:
            return ast.literal_eval(n.value)
    raise AssertionError(name)


def _cols(table):
    return {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in q(
        "select column_name, data_type, is_nullable from information_schema.columns "
        "where table_schema='public' and table_name=%s", (table,))}


def _unique_sets(table):
    rows = q("""select c.conname, array_agg(a.attname order by a.attname) cols
                from pg_constraint c join pg_class t on t.oid=c.conrelid
                join pg_attribute a on a.attrelid=t.oid and a.attnum = any(c.conkey)
                where t.relname=%s and c.contype in ('u','p') group by c.conname""", (table,))
    return {tuple(r["cols"]) for r in rows}


def test_auth_part_is_verbatim_copy():
    assert AUTH in STAGING
    assert STAGING.index("PART 2 - OWNER/PACKER") < STAGING.index(AUTH[:200])


def test_runs_twice_without_error():
    for _ in range(2):
        c = tps.conn(); c.cursor().execute(STAGING); c.close()


def test_application_tables_match_columns_the_code_uses():
    assert _app_const("PACKED_ORDERS_TABLE") == "packed_orders"
    assert _app_const("SNAPSHOT_ITEMS_TABLE") == "packed_snapshot_items"
    assert re.search(r'SUPABASE_TABLE\s*=\s*"shopee_tokens"', SA_SRC)
    # packed_orders: "shop_id,order_number,packed_at"
    po = _cols("packed_orders")
    assert set(po) == {"shop_id", "order_number", "packed_at"}
    assert po["shop_id"][0] == "bigint" and po["order_number"][0] == "text" and po["packed_at"][0] == "timestamp with time zone"
    # packed_snapshot_items: shop_id, order_number, packed_at, row_index + SNAPSHOT_DB_TO_APP
    snap_cols = {"shop_id", "order_number", "packed_at", "row_index", *_app_const("SNAPSHOT_DB_TO_APP")}
    sn = _cols("packed_snapshot_items")
    assert snap_cols <= set(sn), snap_cols - set(sn)
    assert set(sn) - snap_cols == {"id"}                         # the only column the code never touches (docstring-only)
    assert sn["jumlah"][0] == "integer" and sn["row_index"][0] == "integer" and sn["shop_id"][0] == "bigint"
    assert all(sn[c][0] == "text" for c in _app_const("SNAPSHOT_DB_TO_APP") if c != "jumlah")
    # shopee_tokens: the six columns of the select / upsert in shopee_auth.py
    tk = _cols("shopee_tokens")
    assert set(tk) == {"shop_id", "access_token", "refresh_token", "expire_in", "fetch_time", "partner_id"}
    assert all(tk[c][0] == "bigint" for c in ("shop_id", "expire_in", "fetch_time", "partner_id"))
    assert all(tk[c][0] == "text" for c in ("access_token", "refresh_token"))


def test_upsert_conflict_targets_have_unique_constraints():
    assert 'on_conflict="shop_id"' in SA_SRC and ("shop_id",) in _unique_sets("shopee_tokens")
    assert 'on_conflict="shop_id,order_number"' in APP_SRC and ("order_number", "shop_id") in _unique_sets("packed_orders")
    assert 'on_conflict="shop_id,order_number,row_index"' in APP_SRC
    assert ("order_number", "row_index", "shop_id") in _unique_sets("packed_snapshot_items")


def test_rls_enabled_on_every_sensitive_table():
    rows = q("select relname, relrowsecurity from pg_class where relnamespace='public'::regnamespace and relkind='r'")
    got = {r["relname"]: r["relrowsecurity"] for r in rows}
    for t in ("shopee_tokens", "packed_orders", "packed_snapshot_items", "profiles", "packer_accounts"):
        assert got.get(t) is True, t
    assert q("select count(*) c from pg_policies where schemaname='public'")[0]["c"] == 0


def test_anon_and_authenticated_have_no_access_to_any_table():
    for role in ("anon", "authenticated"):
        for t in ("shopee_tokens", "packed_orders", "packed_snapshot_items", "profiles", "packer_accounts"):
            expect_error(f"select * from public.{t}", role=role, contains="permission denied")
        expect_error("insert into public.packed_orders(shop_id,order_number) values (1,'x')", role=role, contains="permission denied")
        expect_error("insert into public.shopee_tokens(shop_id) values (1)", role=role, contains="permission denied")
    leaks = q("""select grantee, table_name, privilege_type from information_schema.role_table_grants
                 where table_schema='public' and grantee in ('anon','authenticated','PUBLIC')""")
    assert leaks == [], leaks


def test_service_role_gets_only_the_granted_privileges():
    grants = {(r["table_name"], r["privilege_type"]) for r in q(
        "select table_name, privilege_type from information_schema.role_table_grants where table_schema='public' and grantee='service_role'")}
    assert {("shopee_tokens", p) for p in ("SELECT", "INSERT", "UPDATE")} <= grants
    assert {("packed_orders", "SELECT"), ("packed_orders", "INSERT"),
            ("packed_snapshot_items", "SELECT"), ("packed_snapshot_items", "INSERT")} <= grants
    for forbidden in (("shopee_tokens", "DELETE"), ("packed_orders", "UPDATE"), ("packed_orders", "DELETE"),
                      ("packed_snapshot_items", "UPDATE"), ("packed_snapshot_items", "DELETE"),
                      ("packed_orders", "TRUNCATE"), ("shopee_tokens", "TRUNCATE")):
        assert forbidden not in grants, forbidden
    expect_error("delete from public.packed_orders", role="service_role", contains="permission denied")
    expect_error("update public.packed_orders set packed_at = now()", role="service_role", contains="permission denied")
    expect_error("delete from public.shopee_tokens", role="service_role", contains="permission denied")
    expect_error("select pin_hash from public.packer_accounts", role="service_role", contains="permission denied")


def test_service_role_has_select_only_on_profiles():
    privs = {r["privilege_type"] for r in q(
        "select privilege_type from information_schema.role_table_grants "
        "where table_schema='public' and table_name='profiles' and grantee='service_role'")}
    assert privs == {"SELECT"}, privs
    for p in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert p not in privs, p
    for sql in ("insert into public.profiles(user_id,email,role) values ('33333333-3333-3333-3333-333333333333','x@x.test','OWNER')",
                "update public.profiles set is_active = false",
                "delete from public.profiles",
                "truncate public.profiles"):
        expect_error(sql, role="service_role", contains="permission denied")
    q("select user_id,email,role,is_active from public.profiles", role="service_role")        # the app's read still works
    # the column set the app actually selects is covered by the table-level SELECT
    assert 'select("user_id,email,role,is_active")' in open(os.path.join(REPO, "auth_guard.py")).read()


def test_part3_comes_after_the_verbatim_auth_part():
    assert STAGING.index(AUTH) + len(AUTH) < STAGING.index("grant select on public.profiles to service_role;")


def test_postgrest_style_upserts_work_as_service_role_and_are_idempotent():
    # same statements PostgREST generates for upsert(..., on_conflict=..., ignore_duplicates=True)
    for _ in range(2):
        q("insert into public.packed_snapshot_items(shop_id,order_number,packed_at,row_index,no_pesanan,jumlah) "
          "values (1272241861,'ORD1',now(),0,'ORD1',2),(1272241861,'ORD1',now(),1,'ORD1',1) "
          "on conflict (shop_id,order_number,row_index) do nothing", role="service_role")
        q("insert into public.packed_orders(shop_id,order_number,packed_at) values (1272241861,'ORD1',now()) "
          "on conflict (shop_id,order_number) do nothing", role="service_role")
    assert q("select count(*) c from public.packed_snapshot_items where order_number='ORD1'", role="service_role")[0]["c"] == 2
    assert q("select count(*) c from public.packed_orders where order_number='ORD1'", role="service_role")[0]["c"] == 1
    # tokens: upsert DO UPDATE
    for tok in ("a", "b"):
        q("insert into public.shopee_tokens(shop_id,access_token,refresh_token,expire_in,fetch_time,partner_id) "
          "values (1272241861,%s,'r',14400,1,1) on conflict (shop_id) do update set access_token=excluded.access_token",
          (tok,), role="service_role")
    assert q("select access_token from public.shopee_tokens", role="service_role")[0]["access_token"] == "b"
    # multi-variant rows stay separate (no merging)
    assert q("select count(distinct row_index) c from public.packed_snapshot_items where order_number='ORD1'")[0]["c"] == 2


def test_guard_aborts_when_application_tables_already_hold_data_unless_overridden():
    assert q("select count(*) c from public.packed_orders")[0]["c"] == 1            # left by the previous test
    c = tps.conn(); cur = c.cursor()
    try:
        cur.execute(STAGING)
    except psycopg2.Error as e:
        assert "ABORTED" in str(e) and "fresh staging project" in str(e)
    else:
        raise AssertionError("guard did not abort")
    finally:
        c.close()
    c = tps.conn(); c.cursor().execute("set onbie.allow_existing_data = 'yes';\n" + STAGING); c.close()
    assert q("select count(*) c from public.packed_orders")[0]["c"] == 1             # nothing lost by the re-run


def test_guard_ignores_empty_or_missing_tables_and_never_drops_anything():
    forbidden = re.compile(r"\b(drop\s+table|truncate\s+table|delete\s+from)\b", re.I)
    for line in STAGING.splitlines():
        code = line.split("--")[0]
        assert not forbidden.search(code), line
    assert "drop schema" not in STAGING.lower()


if __name__ == "__main__":
    if psycopg2 is None:
        print("SKIPPED: psycopg2 not installed"); sys.exit(0)
    tps.PORT = "55441"
    try:
        tps.CLUSTER = tps.Cluster()
        c = tps.CLUSTER.connect(); c.cursor().execute(tps.BOOTSTRAP); c.close()
    except Exception as e:  # noqa: BLE001
        print(f"SKIPPED: cannot start a local PostgreSQL ({type(e).__name__}: {e})"); sys.exit(0)
    order = [n for n in globals() if n.startswith("test_")]
    failed = []
    try:
        for n in order:
            try:
                globals()[n](); print("PASS", n)
            except Exception as e:  # noqa: BLE001
                import traceback; failed.append(n); print("FAIL", n, type(e).__name__, e); traceback.print_exc(limit=3)
        print(f"\n{len(order) - len(failed)}/{len(order)} passed")
    finally:
        tps.CLUSTER.stop()
    sys.exit(1 if failed else 0)
