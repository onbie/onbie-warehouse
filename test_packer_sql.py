"""
REAL-Postgres tests for the PACKER login SQL in supabase_auth_setup.sql
(verification + lockout atomicity, ID assignment, privileges).

This starts a THROWAWAY local PostgreSQL cluster (temp dir, unix socket, no
network) - it never touches Supabase or any production database. Supabase's
own roles/schemas (anon, authenticated, service_role, auth.users, extensions)
are stubbed. Skipped automatically if PostgreSQL server binaries or psycopg2
are not available.

    python3 test_packer_sql.py
"""
import glob
import hashlib
import hmac
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.abspath(__file__))
SQL_FILE = os.path.join(REPO, "supabase_auth_setup.sql")
PEPPER = "test-pepper-" + "x" * 40
PORT = "55439"

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None


def sec(code, pin, pepper=PEPPER):
    return hmac.new(pepper.encode(), f"onbie-pin-v1:{code}:{pin}".encode(), hashlib.sha256).hexdigest()


BOOTSTRAP = """
create role anon nologin; create role authenticated nologin; create role service_role nologin bypassrls;  -- as in Supabase
create schema auth; create table auth.users (id uuid primary key);
create schema extensions;
grant usage on schema public, extensions to anon, authenticated, service_role;
alter default privileges in schema public grant all on tables    to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
"""


class Cluster:
    def __init__(self):
        bins = sorted(glob.glob("/usr/lib/postgresql/*/bin"))
        if not bins:
            raise RuntimeError("no PostgreSQL server binaries")
        self.bin = bins[-1]
        self.tmp = tempfile.mkdtemp(prefix="onbie_pg_")
        self.run_as = []
        if os.geteuid() == 0:
            pw = pwd.getpwnam("postgres")
            os.chown(self.tmp, pw.pw_uid, pw.pw_gid)
            self.run_as = ["runuser", "-u", "postgres", "--"]
        os.chmod(self.tmp, 0o755)
        data = os.path.join(self.tmp, "data")
        self._run("initdb", "-D", data, "-A", "trust", "--no-sync", "-U", "postgres")
        self._run("pg_ctl", "-D", data, "-w", "-l", os.path.join(self.tmp, "log"),
                  "-o", f"-c listen_addresses='' -c unix_socket_directories={self.tmp} -c port={PORT} -c fsync=off", "start")
        self.data = data

    def _run(self, prog, *args):
        subprocess.run(self.run_as + [os.path.join(self.bin, prog), *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def connect(self, autocommit=True):
        c = psycopg2.connect(host=self.tmp, port=PORT, user="postgres", dbname="postgres")
        c.autocommit = autocommit
        return c

    def stop(self):
        try:
            self._run("pg_ctl", "-D", self.data, "-m", "immediate", "stop")
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)


CLUSTER = None


def _setup():
    global CLUSTER
    CLUSTER = Cluster()
    c = CLUSTER.connect()
    cur = c.cursor()
    cur.execute(BOOTSTRAP)
    cur.execute(open(SQL_FILE).read())
    c.close()


def conn():
    return CLUSTER.connect()


def q(sql, args=None, role=None):
    c = conn()
    try:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if role:
            cur.execute(f"set role {role}")
        cur.execute(sql, args)
        try:
            return cur.fetchall()
        except psycopg2.ProgrammingError:
            return None
    finally:
        c.close()


def login(code, pin, pepper=PEPPER):
    return q("select public.packer_login(%s,%s) as r", (code, sec(code, pin, pepper)))[0]["r"]


def login_raw(code, secret):
    return q("select public.packer_login(%s,%s) as r", (code, secret))[0]["r"]


def new_account(pin="482913"):
    code = q("select public.packer_reserve_code() as c")[0]["c"]
    out = q("select public.packer_create(%s,%s) as r", (code, sec(code, pin)))[0]["r"]
    return code, out["user_id"], pin


def row(code):
    return q("select * from public.packer_accounts where user_code=%s", (code,))[0]


def expect_error(sql, args=None, role=None, contains=""):
    try:
        q(sql, args, role)
    except psycopg2.Error as e:
        assert contains.lower() in str(e).lower(), f"error text {str(e)!r} lacks {contains!r}"
        return
    raise AssertionError("expected a database error")


# ----------------------------------------------------------------------
def test_script_is_idempotent_and_loads():
    c = conn()
    c.cursor().execute(open(SQL_FILE).read())   # second run must not fail
    c.close()


def test_ids_are_sequential_and_never_reused():
    a, _, _ = new_account()
    b, _, _ = new_account()
    assert (int(b) - int(a)) == 1 and len(a) == 2 and a != "00"
    wasted = q("select public.packer_reserve_code() as c")[0]["c"]      # reserved, never created
    c, _, _ = new_account()
    assert int(c) > int(wasted), "a reserved-but-unused ID must not be handed out again"


def test_correct_pin_ok_wrong_pin_invalid_and_state_updates():
    code, uid, pin = new_account()
    ok = login(code, pin)
    assert ok["status"] == "ok" and ok["user_code"] == code and ok["user_id"] == uid and ok["session_epoch"] == 0
    assert login(code, "000001")["status"] == "invalid"
    r = row(code)
    assert r["failed_streak"] == 1 and r["window_failures"] == 1 and r["last_failed_at"] is not None


def test_only_bcrypt_of_derived_secret_is_stored_never_the_pin():
    code, _, pin = new_account("135792")
    h = row_with_hash(code)
    assert h.startswith("$2") and len(h) == 60
    dump = subprocess.run(
        ["pg_dump", "-h", CLUSTER.tmp, "-p", PORT, "-U", "postgres", "--data-only", "-t", "public.packer_accounts", "postgres"],
        capture_output=True, text=True, env={**os.environ, "PATH": CLUSTER.bin + os.pathsep + os.environ["PATH"]})
    assert dump.returncode == 0, dump.stderr
    assert pin not in dump.stdout.replace(h, "") and sec(code, pin) not in dump.stdout


def row_with_hash(code):
    return q("select pin_hash from public.packer_accounts where user_code=%s", (code,))[0]["pin_hash"]


def test_temporary_lock_after_5_blocks_even_the_correct_pin_and_does_not_count():
    code, _, pin = new_account()
    for i in range(4):
        assert login(code, "111112")["status"] == "invalid"
    r5 = login(code, "111112")
    assert r5["status"] == "locked" and 0 < r5["retry_after_seconds"] <= 300
    before = row(code)
    for _ in range(5):
        assert login(code, pin)["status"] == "locked"                  # correct PIN is NOT verified while locked
        assert login(code, "999999")["status"] == "locked"
    after = row(code)
    assert (after["failed_streak"], after["window_failures"]) == (before["failed_streak"], before["window_failures"]) == (5, 5)
    q("update public.packer_accounts set locked_until = now() - interval '1 second' where user_code=%s", (code,))
    ok = login(code, pin)
    assert ok["status"] == "ok" and row(code)["failed_streak"] == 0


def test_every_failure_after_5_relocks_and_10_means_30_minutes():
    code, _, pin = new_account()
    for _ in range(5):
        login(code, "222223")
    for n in range(6, 10):                                              # failures 6..9: each re-locks for 5 min
        q("update public.packer_accounts set locked_until = now() - interval '1 second' where user_code=%s", (code,))
        r = login(code, "222223")
        assert r["status"] == "locked" and r["retry_after_seconds"] <= 300, (n, r)
    q("update public.packer_accounts set locked_until = now() - interval '1 second' where user_code=%s", (code,))
    r10 = login(code, "222223")
    assert r10["status"] == "locked" and 300 < r10["retry_after_seconds"] <= 1800
    assert row(code)["failed_streak"] == 10


def _expire_lock(code):
    q("update public.packer_accounts set locked_until = now() - interval '1 second' where user_code=%s", (code,))


def _lock_seconds_left(code):
    return float(q("select extract(epoch from locked_until - now()) s from public.packer_accounts where user_code=%s", (code,))[0]["s"])


def _lock_12h(code):
    for _ in range(15):
        _expire_lock(code)                                              # skip the short waits, not the counters
        r = login(code, "333334")
    return r


def test_15_failures_in_window_lock_for_12_hours():
    code, uid, pin = new_account()
    r = _lock_12h(code)
    assert r["status"] == "locked" and 11 * 3600 < r["retry_after_seconds"] <= 12 * 3600, r
    assert row(code)["window_failures"] == 15 and "hard_locked" not in row(code)
    assert 11.9 * 3600 < _lock_seconds_left(code) <= 12 * 3600
    for _ in range(3):                                                  # while locked: even the right PIN, and nothing is counted
        assert login(code, pin)["status"] == "locked"
        assert login(code, "333335")["status"] == "locked"
    assert row(code)["window_failures"] == 15


def test_12h_lock_expires_by_itself_and_a_further_failure_relocks_12h():
    code, uid, pin = new_account()
    _lock_12h(code)
    _expire_lock(code)                                                  # = 12 hours have passed
    r = login(code, "333336")                                           # window still holds 15 failures -> 16th re-locks 12 h
    assert r["status"] == "locked" and r["retry_after_seconds"] > 11 * 3600 and row(code)["window_failures"] == 16
    _expire_lock(code)
    ok = login(code, pin)                                               # right PIN after expiry works with no OWNER action
    assert ok["status"] == "ok" and row(code)["failed_streak"] == 0 and row(code)["locked_until"] is None
    assert row(code)["window_failures"] == 16                           # a success does not reset the rolling window


def test_owner_can_unlock_a_12h_lock_early():
    code, uid, pin = new_account()
    _lock_12h(code)
    assert login(code, pin)["status"] == "locked"
    q("select public.packer_unlock(%s)", (uid,))
    r = row(code)
    assert (r["failed_streak"], r["window_failures"], r["locked_until"], r["window_started_at"]) == (0, 0, None, None)
    assert login(code, pin)["status"] == "ok"


def test_success_does_not_reset_rolling_window_so_interleaved_logins_cannot_hide_guessing():
    code, _, pin = new_account()
    statuses = []
    for _ in range(6):                                                  # 4 wrong, 1 right, repeat: streak never reaches 5
        for _ in range(4):
            statuses.append(login(code, "444445")["status"])
        if statuses[-1] == "locked":
            break
        assert login(code, pin)["status"] == "ok"
    assert statuses[:12] == ["invalid"] * 12                            # the 5-in-a-row lock never fired
    assert statuses[12:14] == ["invalid", "invalid"] and statuses[14] == "locked"   # 15th verified failure triggers the 12 h lock
    assert statuses[15:] == ["locked"]                                  # 16th attempt: locked, not counted
    r = row(code)
    assert r["window_failures"] == 15 and _lock_seconds_left(code) > 11.9 * 3600
    assert login(code, pin)["status"] == "locked"                       # 12 h, despite the interleaved successes


def test_no_permanent_lock_state_exists():
    cols = {r["column_name"] for r in q("select column_name from information_schema.columns where table_name='packer_accounts'")}
    assert "hard_locked" not in cols
    src = open(SQL_FILE).read()
    assert "'hard_locked'" not in src and "a.hard_locked" not in src


def test_unlock_all_clears_every_lock_and_counter_and_reports_locked_count():
    locked1, uid1, pin1 = new_account()
    locked2, uid2, pin2 = new_account()
    counted, uid3, pin3 = new_account()
    fresh, uid4, pin4 = new_account()
    for _ in range(5):
        login(locked1, "100001")                                        # 5-min lock
    for _ in range(15):
        _expire_lock(locked2)
        login(locked2, "100002")                                        # 12-h lock
    login(counted, "100003"); login(counted, "100003")                  # counters only, not locked
    locked_now = q("select count(*) c from public.packer_accounts where locked_until > now()")[0]["c"]
    assert locked_now >= 2
    assert q("select public.packer_unlock_all() as n")[0]["n"] == locked_now
    for code in (locked1, locked2, counted, fresh):
        r = row(code)
        assert (r["failed_streak"], r["window_failures"], r["locked_until"], r["window_started_at"]) == (0, 0, None, None), code
    for code, pin in ((locked1, pin1), (locked2, pin2), (counted, pin3), (fresh, pin4)):
        assert login(code, pin)["status"] == "ok"
    assert q("select public.packer_unlock_all() as n")[0]["n"] == 0     # nothing locked now


def test_unlock_all_does_not_touch_pins_activation_or_epochs():
    code, uid, pin = new_account()
    q("select public.packer_set_active(%s,false)", (uid,))
    h = row_with_hash(code); before = row(code)
    login(code, "100009")
    q("select public.packer_unlock_all()")
    after = row(code)
    assert row_with_hash(code) == h and after["is_active"] is False and after["session_epoch"] == before["session_epoch"]
    q("select public.packer_set_active(%s,true)", (uid,))


def test_window_rolls_after_30_days():
    code, _, pin = new_account()
    for _ in range(3):
        login(code, "555556")
    q("update public.packer_accounts set window_started_at = now() - interval '31 days' where user_code=%s", (code,))
    login(code, "555556")
    assert row(code)["window_failures"] == 1


def test_inactive_reports_only_after_correct_pin_and_wrong_pins_still_count():
    code, uid, pin = new_account()
    q("select public.packer_set_active(%s, false)", (uid,))
    assert login(code, "666667")["status"] == "invalid" and row(code)["failed_streak"] == 1
    assert login(code, pin)["status"] == "inactive"
    q("select public.packer_set_active(%s, true)", (uid,))
    assert login(code, pin)["status"] == "ok"


def test_unknown_malformed_inputs_are_invalid_and_create_nothing():
    n = q("select count(*) c from public.packer_accounts")[0]["c"]
    assert login_raw("98", sec("98", "123456"))["status"] == "invalid"        # no such account
    for code, secret in (("9", "a" * 64), ("999", "a" * 64), ("ab", "a" * 64), ("07", "short"), ("07", "G" * 64), ("07", None), (None, "a" * 64)):
        assert login_raw(code, secret)["status"] == "invalid"
    assert q("select count(*) c from public.packer_accounts")[0]["c"] == n


def test_concurrent_wrong_guesses_cannot_race_past_the_lock():
    code, uid, pin = new_account()
    statuses, errors = [], []

    def attempt():
        try:
            c = CLUSTER.connect()
            cur = c.cursor()
            cur.execute("select public.packer_login(%s,%s)", (code, sec(code, "777778")))
            statuses.append(cur.fetchone()[0]["status"])
            c.close()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=attempt) for _ in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors[:2]
    r = row(code)
    assert r["failed_streak"] == 5 and r["window_failures"] == 5, r      # exactly 5 verified guesses, never more
    assert statuses.count("invalid") == 4 and statuses.count("locked") == 36, (statuses.count("invalid"), statuses.count("locked"))


def test_concurrent_attempts_across_the_12h_lock_boundary():
    code, uid, pin = new_account()
    q("update public.packer_accounts set window_failures = 12, window_started_at = now() where user_code=%s", (code,))
    out = []

    def attempt():
        c = CLUSTER.connect()
        cur = c.cursor()
        cur.execute("select public.packer_login(%s,%s)", (code, sec(code, "888889")))
        out.append(cur.fetchone()[0]["status"])
        c.close()

    ts = [threading.Thread(target=attempt) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    r = row(code)
    assert r["window_failures"] == 15, r                                  # exactly 3 more guesses verified, never more
    assert out.count("invalid") == 2 and out.count("locked") == 10, (out.count("invalid"), out.count("locked"))
    assert 11.9 * 3600 < _lock_seconds_left(code) <= 12 * 3600


def test_concurrent_unlock_all_and_failed_logins_do_not_deadlock_or_corrupt():
    code, uid, pin = new_account()
    errors = []

    def guess():
        try:
            for _ in range(3):
                login(code, "999990")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def unlock():
        try:
            for _ in range(3):
                q("select public.packer_unlock_all()")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=guess) for _ in range(4)] + [threading.Thread(target=unlock) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors[:2]
    r = row(code)
    assert 0 <= r["failed_streak"] <= 12 and 0 <= r["window_failures"] <= 12
    q("select public.packer_unlock_all()")
    assert login(code, pin)["status"] == "ok"


def test_set_pin_rotates_hash_kills_sessions_and_clears_locks():
    code, uid, pin = new_account()
    for _ in range(5):
        login(code, "121213")
    q("select public.packer_set_pin(%s,%s)", (uid, sec(code, "654987")))
    r = row(code)
    assert r["session_epoch"] == 1 and r["failed_streak"] == 0 and r["locked_until"] is None
    assert login(code, pin)["status"] == "invalid"
    ok = login(code, "654987")
    assert ok["status"] == "ok" and ok["session_epoch"] == 1


def test_disable_bumps_epoch_enable_does_not():
    code, uid, pin = new_account()
    q("select public.packer_set_active(%s,false)", (uid,)); assert row(code)["session_epoch"] == 1
    q("select public.packer_set_active(%s,true)", (uid,)); assert row(code)["session_epoch"] == 1


def test_admin_functions_fail_for_unknown_account_and_bad_secret():
    ghost = "00000000-0000-0000-0000-000000000000"
    for sql in ("select public.packer_unlock(%s)", "select public.packer_set_active(%s, false)"):
        expect_error(sql, (ghost,), contains="not found")
    expect_error("select public.packer_set_pin(%s,%s)", (ghost, sec("01", "123456")), contains="not found")
    code, uid, _ = new_account()
    expect_error("select public.packer_set_pin(%s,%s)", (uid, "not-hex"), contains="invalid secret")


def test_create_rules_reserved_code_format_and_duplicates():
    s = sec("50", "123456")
    expect_error("select public.packer_create(%s,%s)", ("00", s), contains="invalid packer code")
    expect_error("select public.packer_create(%s,%s)", ("5", s), contains="invalid packer code")
    expect_error("select public.packer_create(%s,%s)", ("99", s), contains="not reserved")
    code = q("select public.packer_reserve_code() as c")[0]["c"]
    expect_error("select public.packer_create(%s,%s)", (code, "x" * 64), contains="invalid secret")
    q("select public.packer_create(%s,%s)", (code, sec(code, "123456")))
    expect_error("select public.packer_create(%s,%s)", (code, sec(code, "123456")), contains="duplicate")


def test_accounts_are_never_deleted_and_code_is_immutable():
    code, uid, _ = new_account()
    expect_error("delete from public.packer_accounts where user_code=%s", (code,), contains="never deleted")
    expect_error("truncate public.packer_accounts", contains="never deleted")
    expect_error("update public.packer_accounts set user_code='98' where user_code=%s", (code,), contains="immutable")


def test_privileges_service_role_cannot_read_pin_hash_or_write_directly():
    code, uid, pin = new_account()
    expect_error("select pin_hash from public.packer_accounts", role="service_role", contains="permission denied")
    expect_error("select * from public.packer_accounts", role="service_role", contains="permission denied")
    expect_error("update public.packer_accounts set is_active=false", role="service_role", contains="permission denied")
    expect_error("insert into public.packer_accounts(user_code,pin_hash) values ('97','x')", role="service_role", contains="permission denied")
    expect_error("delete from public.packer_accounts", role="service_role", contains="permission denied")
    rows = q("select user_code, is_active, session_epoch from public.packer_accounts where user_code=%s", (code,), role="service_role")
    assert rows[0]["user_code"] == code
    r = q("select public.packer_login(%s,%s) as r", (code, sec(code, pin)), role="service_role")[0]["r"]
    assert r["status"] == "ok"                                          # service_role can only go through the function


def test_privileges_anon_and_logged_in_users_get_nothing():
    code, uid, pin = new_account()
    for role in ("anon", "authenticated"):
        expect_error("select public.packer_login(%s,%s)", (code, sec(code, pin)), role=role, contains="permission denied")
        expect_error("select public.packer_reserve_code()", role=role, contains="permission denied")
        expect_error("select public.packer_unlock(%s)", (uid,), role=role, contains="permission denied")
        expect_error("select public.packer_unlock_all()", role=role, contains="permission denied")
        expect_error("select * from public.packer_accounts", role=role, contains="permission denied")
        expect_error("select * from public.profiles", role=role, contains="permission denied")


def test_profiles_accepts_owner_only():
    c = conn()
    cur = c.cursor()
    cur.execute("insert into auth.users(id) values ('11111111-1111-1111-1111-111111111111') on conflict do nothing")
    cur.execute("insert into public.profiles(user_id,email,role) values ('11111111-1111-1111-1111-111111111111','o@x.test','OWNER') on conflict do nothing")
    cur.execute("insert into auth.users(id) values ('22222222-2222-2222-2222-222222222222') on conflict do nothing")
    try:
        cur.execute("insert into public.profiles(user_id,email,role) values ('22222222-2222-2222-2222-222222222222','p@x.test','PACKER')")
    except psycopg2.Error as e:
        assert "check" in str(e).lower()
    else:
        raise AssertionError("profiles must reject non-OWNER roles")
    finally:
        c.close()


def test_id_space_exhaustion_is_an_error_and_runs_last():
    # consume the rest of 01..99, then the next reservation must fail (no cycle, no reuse)
    seen = set(r["user_code"] for r in q("select user_code from public.packer_accounts"))
    while True:
        try:
            code = q("select public.packer_reserve_code() as c")[0]["c"]
        except psycopg2.Error as e:
            assert "maximum" in str(e).lower() or "reached" in str(e).lower()
            break
        assert code not in seen
        seen.add(code)
    assert max(int(c) for c in seen) == 99


# ----------------------------------------------------------------------
def _collect():
    return [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]


if __name__ == "__main__":
    if psycopg2 is None:
        print("SKIPPED: psycopg2 not installed"); sys.exit(0)
    try:
        _setup()
    except Exception as e:  # noqa: BLE001
        print(f"SKIPPED: cannot start a local PostgreSQL ({type(e).__name__}: {e})"); sys.exit(0)
    failed = []
    try:
        tests = _collect()   # definition order; exhaustion test is last on purpose
        for name, fn in tests:
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failed.append(name)
                import traceback
                print(f"FAIL {name}: {type(e).__name__}: {e}")
                traceback.print_exc(limit=3)
        print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    finally:
        CLUSTER.stop()
    sys.exit(1 if failed else 0)
