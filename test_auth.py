"""
Automated tests for login (auth_guard.py, account_admin.py) and its wiring in
app.py / pages/tiktok_callback.py:
  * OWNER  = Supabase Auth email + password
  * PACKER = 2-digit ID + 6-digit PIN (verified/locked by Postgres packer_login)

NO live Supabase: every test uses in-memory FAKE clients. Nothing here is,
or becomes, a fallback inside the application - the production modules
contain no fake login or test mode; tests only monkeypatch module attributes.
The SQL itself (lockout atomicity, privileges, ID assignment) is tested on a
real throwaway PostgreSQL in test_packer_sql.py.

Run from the repo root (needs streamlit + supabase for the AppTest /
real-library sections; those are skipped if missing):

    python3 test_auth.py            # plain runner
    python3 -m pytest test_auth.py  # also works
"""
import base64
import json
import logging
import os
import re
import sys
import types

REPO = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
os.chdir(REPO)

import auth_guard  # noqa: E402
import account_admin  # noqa: E402

ORIG_SESSION = auth_guard._session
ORIG_NEW_AUTH_CLIENT = auth_guard._new_auth_client
ORIG_DATA_CLIENT = auth_guard._data_client
ORIG_CLOCK = auth_guard._clock

URL = "https://abcdefgh.supabase.co"
ONBIE, GOTTO = 1272241861, 1179306776
PEPPER = "unit-test-pepper-" + "p" * 40
HEX64 = re.compile(r"[0-9a-f]{64}")


def _jwt(role):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'HS256'})}.{b({'role': role})}.c2ln"


ANON_KEY = _jwt("anon")
SERVICE_KEY = _jwt("service_role")

OWNER_ID = "uid-owner"
OWNER_EMAIL, OWNER_PW = "owner@onbie.test", "OwnerPass123"
PACKER_UUID = "00000000-0000-0000-0000-000000000007"     # ID 07, active
INACTIVE_UUID = "00000000-0000-0000-0000-000000000008"   # ID 08, disabled
PACKER_CODE, PACKER_PIN = "07", "482913"
INACTIVE_CODE, INACTIVE_PIN = "08", "135792"


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------
class AuthApiError(Exception):          # same class NAME as gotrue's
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


class AuthRetryableError(Exception):
    status = 502


class Resp:
    def __init__(self, data):
        self.data = data


class Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.op, self.cols, self.filters, self.orders = "select", None, [], []
        self.lim = self.rng = None

    def select(self, cols="*"):
        if self.name == "packer_accounts":
            assert cols != "*" and "pin_hash" not in cols, f"the app must never select pin_hash/*: {cols!r}"
        self.op, self.cols = "select", (None if cols == "*" else [c.strip() for c in cols.split(",")])
        return self

    def eq(self, c, v): self.filters.append((c, v)); return self
    def order(self, c, desc=False): self.orders.append((c, desc)); return self
    def limit(self, n): self.lim = n; return self
    def range(self, lo, hi): self.rng = (lo, hi); return self

    def upsert(self, payload, on_conflict="", ignore_duplicates=False):
        self.op, self.payload = "upsert", payload if isinstance(payload, list) else [payload]
        self.on_conflict, self.ignore = on_conflict, ignore_duplicates
        return self

    def execute(self):
        db = self.db
        db.log.append((self.op, self.name))
        if db.fail and db.fail(self.op, self.name):
            raise RuntimeError(f"injected {self.op} failure on {self.name}")
        rows = db.tables.setdefault(self.name, [])
        match = lambda r: all(r.get(c) == v for c, v in self.filters)
        if self.op == "upsert":                        # packed_* tables only
            keys = tuple(self.on_conflict.split(",")) if self.on_conflict else None
            for p in self.payload:
                dup = keys and any(all(r.get(k) == p.get(k) for k in keys) for r in rows)
                if dup and self.ignore:
                    continue
                rows.append(dict(p))
            return Resp([])
        out = [r for r in rows if match(r)]
        for c, desc in reversed(self.orders):
            out.sort(key=lambda r: (r.get(c) is None, r.get(c)), reverse=desc)
        if self.rng:
            out = out[self.rng[0]: self.rng[1] + 1]
        if self.lim is not None:
            out = out[: self.lim]
        return Resp([{c: r.get(c) for c in self.cols} if self.cols else dict(r) for r in out])


class Rpc:
    def __init__(self, fn): self.fn = fn
    def execute(self): return Resp(self.fn())


class FakeDB:
    """Stands in for the shared service-role data client. `rpc()` mimics the
    PLUMBING of the Postgres functions (statuses, shapes); lockout/atomicity
    semantics are covered by test_packer_sql.py on a real Postgres."""

    def __init__(self, derive):
        self.derive = derive
        self.tables = {"profiles": [], "packer_accounts": [], "shopee_tokens": [],
                       "packed_orders": [], "packed_snapshot_items": []}
        self.log, self.fail, self.rpc_log = [], None, []
        self.rpc_fail, self.rpc_override = None, {}
        self.code_seq = 8
        self.auth = types.SimpleNamespace(sign_in_with_password=self._forbidden)
        self.sign_in_calls = 0

    def _forbidden(self, *a, **k):
        self.sign_in_calls += 1
        raise AssertionError("sign_in_with_password called on the shared data client")

    def table(self, name):
        return Query(self, name)

    # --- RPC plumbing --------------------------------------------------
    def rpc(self, name, params):
        self.rpc_log.append((name, dict(params)))

        def run():
            if self.rpc_fail and self.rpc_fail(name):
                raise RuntimeError(f"injected rpc failure: {name} secret={params.get('p_secret', '')}")
            if name in self.rpc_override:
                v = self.rpc_override[name]
                return v(params) if callable(v) else v
            return getattr(self, "_rpc_" + name)(params)
        return Rpc(run)

    def _acct(self, **kw):
        for a in self.tables["packer_accounts"]:
            if all(a.get(k) == v for k, v in kw.items()):
                return a
        return None

    def _rpc_packer_login(self, p):
        a = self._acct(user_code=p["p_code"])
        if not a or a["_secret"] != p["p_secret"]:
            return {"status": "invalid"}
        if not a["is_active"]:
            return {"status": "inactive"}
        return {"status": "ok", "user_id": a["user_id"], "user_code": a["user_code"], "session_epoch": a["session_epoch"]}

    def _rpc_packer_reserve_code(self, p):
        self.code_seq += 1
        return f"{self.code_seq:02d}"

    def _rpc_packer_create(self, p):
        assert HEX64.fullmatch(p["p_secret"]), "secret must be a 64-hex HMAC, not a PIN"
        uid = f"00000000-0000-0000-0000-{int(p['p_code']):012d}"
        self.tables["packer_accounts"].append({
            "user_id": uid, "user_code": p["p_code"], "is_active": True, "session_epoch": 0,
            "failed_streak": 0, "window_failures": 0, "locked_until": None,
            "last_failed_at": None, "created_at": "2026-10-25", "_secret": p["p_secret"]})
        return {"user_id": uid, "user_code": p["p_code"]}

    def _rpc_packer_set_pin(self, p):
        a = self._acct(user_id=p["p_user_id"]); assert a
        a.update(_secret=p["p_secret"], session_epoch=a["session_epoch"] + 1, failed_streak=0,
                 window_failures=0, locked_until=None)

    def _rpc_packer_set_active(self, p):
        a = self._acct(user_id=p["p_user_id"]); assert a
        a["is_active"] = p["p_active"]
        if not p["p_active"]:
            a["session_epoch"] += 1

    def _rpc_packer_unlock(self, p):
        a = self._acct(user_id=p["p_user_id"]); assert a
        a.update(failed_streak=0, window_failures=0, locked_until=None)

    def _rpc_packer_unlock_all(self, p):
        n = 0
        for a in self.tables["packer_accounts"]:
            n += 1 if a.get("locked_until") else 0
            a.update(failed_streak=0, window_failures=0, locked_until=None)
        return n


class FakeAuthClient:
    def __init__(self, tracker):
        self.tracker = tracker
        self.auth = types.SimpleNamespace(sign_in_with_password=self._sign_in)

    def _sign_in(self, creds):
        self.tracker["sign_ins"] += 1
        if self.tracker.get("outage"):
            raise AuthRetryableError("network down")
        if self.tracker.get("rate_limited"):
            raise AuthApiError("rate limit", status=429)
        if creds["email"] != OWNER_EMAIL or creds["password"] != OWNER_PW:
            raise AuthApiError("Invalid login credentials", status=400)
        return types.SimpleNamespace(user=types.SimpleNamespace(id=OWNER_ID))


class _LogCatcher(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class Env:
    """Per-test fixture: valid env, fake clients, dict session, fake clock."""

    def __init__(self, use_real_session=False):
        os.environ.update(SUPABASE_URL=URL, SUPABASE_KEY=SERVICE_KEY, SUPABASE_ANON_KEY=ANON_KEY, PIN_PEPPER=PEPPER)
        self.db = FakeDB(auth_guard.derive_pin_secret)
        self.db.tables["profiles"] = [{"user_id": OWNER_ID, "email": OWNER_EMAIL, "role": "OWNER", "is_active": True}]
        d = auth_guard.derive_pin_secret
        self.db.tables["packer_accounts"] = [
            {"user_id": PACKER_UUID, "user_code": PACKER_CODE, "is_active": True, "session_epoch": 3,
             "failed_streak": 0, "window_failures": 0, "locked_until": None,
             "last_failed_at": None, "created_at": "2026-10-01", "_secret": d(PACKER_CODE, PACKER_PIN)},
            {"user_id": INACTIVE_UUID, "user_code": INACTIVE_CODE, "is_active": False, "session_epoch": 0,
             "failed_streak": 0, "window_failures": 0, "locked_until": None,
             "last_failed_at": None, "created_at": "2026-10-02", "_secret": d(INACTIVE_CODE, INACTIVE_PIN)},
        ]
        self.tracker = {"sign_ins": 0, "clients_made": 0}
        self.store = {}
        self.now = [1_000_000.0]
        self.logs = _LogCatcher()
        logging.getLogger().addHandler(self.logs)
        self._old_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)

        def new_client():
            self.tracker["clients_made"] += 1
            return FakeAuthClient(self.tracker)

        auth_guard._new_auth_client = new_client
        auth_guard._data_client = lambda: self.db
        auth_guard._clock = lambda: self.now[0]
        auth_guard._session = ORIG_SESSION if use_real_session else (lambda: self.store)
        auth_guard._reset_throttle_for_tests()

    def login_owner(self):
        return auth_guard.authenticate(OWNER_EMAIL, OWNER_PW)

    def login_packer(self, code=PACKER_CODE, pin=PACKER_PIN):
        return auth_guard.authenticate_pin(code, pin)

    def login(self, who):
        return self.login_owner() if who == "owner" else self.login_packer()

    def close(self):
        logging.getLogger().removeHandler(self.logs)
        logging.getLogger().setLevel(self._old_level)
        auth_guard._new_auth_client = ORIG_NEW_AUTH_CLIENT
        auth_guard._data_client = ORIG_DATA_CLIENT
        auth_guard._clock = ORIG_CLOCK
        auth_guard._session = ORIG_SESSION
        auth_guard._reset_throttle_for_tests()
        for n in ("SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_ANON_KEY", "PIN_PEPPER"):
            os.environ.pop(n, None)


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc as e:
        return e
    raise AssertionError(f"expected {exc.__name__}, nothing raised")


def with_env(fn):
    def wrapper():
        env = Env()
        try:
            fn(env)
        finally:
            env.close()
    wrapper.__name__ = fn.__name__
    return wrapper


def all_strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from all_strings(k); yield from all_strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from all_strings(v)
    else:
        yield str(obj)


# ======================================================================
# A. Configuration (fail closed)
# ======================================================================
@with_env
def test_config_valid(env):
    auth_guard.check_config()


@with_env
def test_config_missing_names_only(env):
    for name in auth_guard.REQUIRED_ENV:
        saved = os.environ.pop(name)
        e = raises(auth_guard.AuthConfigError, auth_guard.check_config)
        assert name in str(e)
        for secret in (SERVICE_KEY, ANON_KEY, PEPPER):
            assert secret not in str(e)
        os.environ[name] = saved


@with_env
def test_config_rejects_key_and_pepper_mixups(env):
    os.environ["SUPABASE_ANON_KEY"] = SERVICE_KEY
    raises(auth_guard.AuthConfigError, auth_guard.check_config)
    os.environ["SUPABASE_ANON_KEY"] = _jwt("service_role") + "x"
    raises(auth_guard.AuthConfigError, auth_guard.check_config)
    os.environ["SUPABASE_ANON_KEY"] = ANON_KEY
    os.environ["SUPABASE_KEY"] = _jwt("anon") + "y"
    raises(auth_guard.AuthConfigError, auth_guard.check_config)
    os.environ["SUPABASE_KEY"] = SERVICE_KEY
    os.environ["PIN_PEPPER"] = "short"
    e = raises(auth_guard.AuthConfigError, auth_guard.check_config)
    assert "short" not in str(e).replace("terlalu pendek", "")
    os.environ["PIN_PEPPER"] = SERVICE_KEY
    raises(auth_guard.AuthConfigError, auth_guard.check_config)


@with_env
def test_both_login_paths_fail_closed_without_config(env):
    os.environ.pop("PIN_PEPPER")
    raises(auth_guard.AuthConfigError, env.login_packer)
    raises(auth_guard.AuthConfigError, env.login_owner)
    assert env.db.rpc_log == [] and env.tracker["sign_ins"] == 0 and env.store == {}


# ======================================================================
# B. OWNER login (email + password)
# ======================================================================
@with_env
def test_owner_login(env):
    s = env.login_owner()
    assert s["role"] == "OWNER" and s["kind"] == "owner" and s["user_id"] == OWNER_ID and s["label"] == OWNER_EMAIL
    assert auth_guard.current_user()["role"] == "OWNER"


@with_env
def test_owner_session_holds_no_secrets(env):
    env.login_owner()
    blob = json.dumps(env.store)
    assert OWNER_PW not in blob and "token" not in blob.lower() and "password" not in blob.lower()


@with_env
def test_owner_wrong_password_and_unknown_email_look_identical(env):
    e1 = raises(auth_guard.AuthError, auth_guard.authenticate, OWNER_EMAIL, "WrongPass123")
    e2 = raises(auth_guard.AuthError, auth_guard.authenticate, "nobody@onbie.test", "WrongPass123")
    assert str(e1) == str(e2) == "Email atau password salah."
    assert env.store == {}


@with_env
def test_email_login_is_owner_only_and_needs_an_active_profile(env):
    p = env.db.tables["profiles"][0]
    for bad in ({"role": "PACKER"}, {"role": "ADMIN"}, {"is_active": False}, {"is_active": "true"}):
        saved = dict(p); p.update(bad)
        raises(auth_guard.AuthError, env.login_owner)
        assert env.store == {}, bad
        p.clear(); p.update(saved)
    env.db.tables["profiles"].clear()
    raises(auth_guard.AuthError, env.login_owner)             # no profile


@with_env
def test_owner_profile_db_failure_fails_closed(env):
    env.db.fail = lambda op, t: t == "profiles"
    e = raises(auth_guard.AuthError, env.login_owner)
    assert "tidak dapat memverifikasi" in str(e).lower() and env.store == {}


@with_env
def test_owner_outage_is_not_a_credential_failure(env):
    env.tracker["outage"] = True
    for _ in range(auth_guard.MAX_FAILED_ATTEMPTS + 2):
        assert "tidak tersedia" in str(raises(auth_guard.AuthError, env.login_owner))
    env.tracker["outage"] = False
    assert env.login_owner()["role"] == "OWNER"


@with_env
def test_owner_email_normalized_and_empty_rejected(env):
    assert auth_guard.authenticate("  OWNER@Onbie.Test ", OWNER_PW)["user_id"] == OWNER_ID
    env.store.clear(); before = env.tracker["sign_ins"]
    raises(auth_guard.AuthError, auth_guard.authenticate, "", "x")
    raises(auth_guard.AuthError, auth_guard.authenticate, OWNER_EMAIL, "")
    assert env.tracker["sign_ins"] == before


# ======================================================================
# C. PACKER login (ID + PIN)
# ======================================================================
@with_env
def test_packer_login_success_session_shape(env):
    s = env.login_packer()
    assert (s["role"], s["kind"], s["label"], s["user_id"], s["epoch"]) == ("PACKER", "packer", "ID 07", PACKER_UUID, 3)
    assert auth_guard.current_user()["role"] == "PACKER"
    assert set(env.store[auth_guard.SESSION_KEY]) == {"user_id", "role", "kind", "label", "epoch", "login_at", "last_active", "last_check"}


@with_env
def test_packer_format_validation_happens_before_any_network_call(env):
    for code, pin in (("7", PACKER_PIN), ("007", PACKER_PIN), ("ab", PACKER_PIN), ("00", PACKER_PIN), ("07", "12345"),
                      ("07", "1234567"), ("07", "12345a"), ("07", ""), ("", PACKER_PIN), (None, None), ("0 7", PACKER_PIN)):
        e = raises(auth_guard.AuthError, auth_guard.authenticate_pin, code, pin)
        assert "2 digit" in str(e) and "6 digit" in str(e)
    assert env.db.rpc_log == [] and env.store == {}


@with_env
def test_raw_pin_is_never_sent_stored_or_logged(env):
    env.login_packer()
    raises(auth_guard.AuthError, auth_guard.authenticate_pin, PACKER_CODE, "999111")       # wrong PIN
    env.db.rpc_fail = lambda n: n == "packer_login"
    raises(auth_guard.AuthError, auth_guard.authenticate_pin, PACKER_CODE, "246802")       # rpc error path (error text carries the secret)
    env.db.rpc_fail = None
    for name, params in env.db.rpc_log:
        assert name == "packer_login" and set(params) == {"p_code", "p_secret"}
        assert HEX64.fullmatch(params["p_secret"]) and params["p_code"] == PACKER_CODE
        for pin in (PACKER_PIN, "999111", "246802"):
            assert pin not in params["p_secret"] or True                         # hex digest may contain digits by chance
            assert pin not in {v for v in params.values()}
    assert env.db.rpc_log[0][1]["p_secret"] == auth_guard.derive_pin_secret(PACKER_CODE, PACKER_PIN)
    log_blob = "\n".join(env.logs.lines)
    store_blob = json.dumps(env.store)
    for secret in (PACKER_PIN, "999111", "246802", PEPPER, SERVICE_KEY, ANON_KEY) + tuple(p["p_secret"] for _, p in env.db.rpc_log):
        assert secret not in log_blob and secret not in store_blob, secret[:8]


def test_derived_secret_properties():
    os.environ["PIN_PEPPER"] = PEPPER
    try:
        a = auth_guard.derive_pin_secret("07", "482913")
        assert HEX64.fullmatch(a) and a == auth_guard.derive_pin_secret("07", "482913")      # deterministic
        assert a != auth_guard.derive_pin_secret("08", "482913")                             # bound to the ID
        assert a != auth_guard.derive_pin_secret("07", "482914")
        os.environ["PIN_PEPPER"] = "another-pepper-" + "q" * 40
        assert a != auth_guard.derive_pin_secret("07", "482913")                             # needs the pepper
        os.environ["PIN_PEPPER"] = "short"
        raises(auth_guard.AuthConfigError, auth_guard.derive_pin_secret, "07", "482913")
    finally:
        os.environ.pop("PIN_PEPPER", None)


@with_env
def test_packer_status_mapping_and_messages(env):
    for code, pin, needle in (("07", "000001", "ID atau PIN salah"), ("99", "123456", "ID atau PIN salah"),
                              ("08", INACTIVE_PIN, "tidak aktif")):
        e = raises(auth_guard.AuthError, auth_guard.authenticate_pin, code, pin)
        assert needle in str(e) and env.store == {}, (code, str(e))
    env.db.rpc_override["packer_login"] = {"status": "locked", "retry_after_seconds": 61}
    assert "2 menit" in str(raises(auth_guard.AuthError, env.login_packer))
    env.db.rpc_override["packer_login"] = {"status": "locked", "retry_after_seconds": 5}
    assert "1 menit" in str(raises(auth_guard.AuthError, env.login_packer))
    env.db.rpc_override["packer_login"] = {"status": "locked", "retry_after_seconds": 12 * 3600 - 7}
    msg = str(raises(auth_guard.AuthError, env.login_packer))
    assert "12 jam" in msg and "OWNER" in msg and "menit" not in msg          # the 12 h lock says hours + that an OWNER can open it
    env.db.rpc_override["packer_login"] = {"status": "locked", "retry_after_seconds": 30 * 60}
    assert "30 menit" in str(raises(auth_guard.AuthError, env.login_packer))
    env.db.rpc_override["packer_login"] = {"status": "hard_locked"}           # no such status any more: fail closed
    assert "tidak tersedia" in str(raises(auth_guard.AuthError, env.login_packer))
    assert env.store == {}


@with_env
def test_packer_unexpected_or_malformed_responses_fail_closed(env):
    good = {"status": "ok", "user_id": PACKER_UUID, "user_code": PACKER_CODE, "session_epoch": 3}
    bads = [None, [], "ok", {}, {"status": "weird"}, {"status": "OK", **{k: v for k, v in good.items() if k != "status"}},
            dict(good, user_id=""), dict(good, user_id=None), dict(good, user_code="08"), dict(good, session_epoch="3"),
            {k: v for k, v in good.items() if k != "session_epoch"}, {"status": "ok"}]
    for bad in bads:
        env.db.rpc_override["packer_login"] = bad
        raises(auth_guard.AuthError, env.login_packer)
        assert env.store == {}, bad
    env.db.rpc_override.clear()
    env.db.rpc_fail = lambda n: True
    e = raises(auth_guard.AuthError, env.login_packer)
    assert "tidak tersedia" in str(e) and PACKER_PIN not in str(e) and not HEX64.search(str(e)) and env.store == {}


@with_env
def test_packer_path_uses_neither_supabase_auth_nor_the_inmemory_throttle(env):
    for _ in range(auth_guard.MAX_FAILED_ATTEMPTS * 3):                 # many failures: no client-side lockout message
        e = raises(auth_guard.AuthError, auth_guard.authenticate_pin, PACKER_CODE, "000001")
        assert "ID atau PIN salah" in str(e)
    assert env.login_packer()["role"] == "PACKER"                       # decision is purely the database's
    assert env.tracker["clients_made"] == 0 and env.tracker["sign_ins"] == 0 and env.db.sign_in_calls == 0


# ======================================================================
# D. Client isolation (the global-client Authorization-header hazard)
# ======================================================================
@with_env
def test_owner_sign_in_never_touches_shared_data_client(env):
    for _ in range(3):
        env.login_owner(); env.store.clear()
    assert env.db.sign_in_calls == 0 and env.tracker["clients_made"] == 3 and env.tracker["sign_ins"] == 3


def test_real_library_clients_do_not_share_authorization_header():
    try:
        from supabase import create_client
    except ImportError:
        print("  (skipped: supabase not installed)"); return
    data = create_client(URL, SERVICE_KEY)
    tmp = create_client(URL, ANON_KEY)
    before = data.options.headers["Authorization"]
    tmp._listen_to_auth_events("SIGNED_IN", types.SimpleNamespace(access_token="USER.JWT.TOKEN"))
    assert tmp.options.headers["Authorization"] == "Bearer USER.JWT.TOKEN"
    assert data.options.headers["Authorization"] == before


def test_real_new_auth_client_is_fresh_anon_and_not_the_data_client():
    try:
        import supabase  # noqa: F401
    except ImportError:
        print("  (skipped: supabase not installed)"); return
    env = Env()
    try:
        auth_guard._new_auth_client = ORIG_NEW_AUTH_CLIENT
        a, b = auth_guard._new_auth_client(), auth_guard._new_auth_client()
        assert a is not b
        assert ANON_KEY in a.options.headers["Authorization"] and SERVICE_KEY not in a.options.headers["Authorization"]
    finally:
        env.close()


# ======================================================================
# E. OWNER in-memory throttle (best effort; PACKER lockout is in Postgres)
# ======================================================================
@with_env
def test_owner_lockout_after_repeated_failures_then_expires(env):
    for _ in range(auth_guard.MAX_FAILED_ATTEMPTS):
        raises(auth_guard.AuthError, auth_guard.authenticate, OWNER_EMAIL, "WrongPass123")
    before = env.tracker["sign_ins"]
    e = raises(auth_guard.AuthError, auth_guard.authenticate, OWNER_EMAIL, OWNER_PW)
    assert "terlalu banyak" in str(e).lower() and env.tracker["sign_ins"] == before
    env.now[0] += auth_guard.LOCKOUT_SECONDS + 1
    assert env.login_owner()["role"] == "OWNER"


@with_env
def test_owner_success_resets_failure_count(env):
    for _ in range(auth_guard.MAX_FAILED_ATTEMPTS - 1):
        raises(auth_guard.AuthError, auth_guard.authenticate, OWNER_EMAIL, "WrongPass123")
    env.login_owner(); env.store.clear()
    for _ in range(auth_guard.MAX_FAILED_ATTEMPTS - 1):
        raises(auth_guard.AuthError, auth_guard.authenticate, OWNER_EMAIL, "WrongPass123")
    assert env.login_owner()["role"] == "OWNER"


@with_env
def test_supabase_rate_limit_message(env):
    env.tracker["rate_limited"] = True
    assert "terlalu banyak" in str(raises(auth_guard.AuthError, env.login_owner)).lower()


# ======================================================================
# F. Session lifecycle
# ======================================================================
@with_env
def test_idle_and_absolute_timeouts(env):
    for who in ("owner", "packer"):
        env.store.clear(); env.login(who)
        env.now[0] += auth_guard.IDLE_TIMEOUT_SECONDS + 1
        assert auth_guard.current_user() is None and auth_guard.SESSION_KEY not in env.store
    env.store.clear(); env.login("packer")
    user = True
    for _ in range(4):
        env.now[0] += auth_guard.MAX_SESSION_SECONDS / 3
        user = auth_guard.current_user(touch=True)
        if user is None:
            break
    assert user is None


@with_env
def test_disabled_packer_is_locked_out_on_next_revalidation(env):
    env.login_packer()
    env.db.tables["packer_accounts"][0]["is_active"] = False
    n = len(env.db.log)
    assert auth_guard.current_user() is not None and len(env.db.log) == n      # within 60 s: no DB hit
    env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
    assert auth_guard.current_user() is None and auth_guard.SESSION_KEY not in env.store
    raises(auth_guard.NotAuthenticated, auth_guard.require_role, "PACKER")


@with_env
def test_pin_reset_epoch_bump_ends_live_packer_sessions(env):
    env.login_packer()
    env.db.tables["packer_accounts"][0]["session_epoch"] += 1
    env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
    assert auth_guard.current_user() is None


@with_env
def test_owner_downgraded_or_disabled_loses_session(env):
    for change in ({"role": "PACKER"}, {"is_active": False}):
        env.store.clear(); env.login_owner()
        p = env.db.tables["profiles"][0]; saved = dict(p); p.update(change)
        env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
        assert auth_guard.current_user() is None, change
        p.clear(); p.update(saved)


@with_env
def test_revalidation_db_failure_fails_closed_and_keeps_session(env):
    for who, table in (("packer", "packer_accounts"), ("owner", "profiles")):
        env.store.clear(); env.login(who)
        env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
        env.db.fail = lambda op, t: t == table
        raises(auth_guard.AuthError, auth_guard.require_role)
        env.db.fail = None
        assert auth_guard.require_role()["role"] == who.upper()


@with_env
def test_tampered_session_is_rejected(env):
    env.login_packer()
    sess = env.store[auth_guard.SESSION_KEY]
    bads = [dict(sess, role="OWNER"),                           # PACKER session claiming OWNER
            dict(sess, kind="owner"),                            # kind/role mismatch
            dict(sess, kind="admin"), dict(sess, role="SUPERUSER"), dict(sess, role="owner"),
            {"role": "OWNER"}, dict(sess, user_id=""), dict(sess, last_check="now"),
            dict(sess, epoch="3"), dict(sess, epoch=True), dict(sess, label=None),
            {k: v for k, v in sess.items() if k != "epoch"}, "OWNER", ["OWNER"]]
    for bad in bads:
        env.store[auth_guard.SESSION_KEY] = bad
        assert auth_guard.current_user() is None and auth_guard.SESSION_KEY not in env.store, bad
        raises(auth_guard.NotAuthenticated, auth_guard.require_role)


@with_env
def test_touch_flag_controls_activity_timestamp(env):
    env.login_packer()
    t0 = env.store[auth_guard.SESSION_KEY]["last_active"]
    env.now[0] += 100
    auth_guard.require_role(touch=False)
    assert env.store[auth_guard.SESSION_KEY]["last_active"] == t0
    auth_guard.require_role(touch=True)
    assert env.store[auth_guard.SESSION_KEY]["last_active"] == t0 + 100


@with_env
def test_logout_clear_all(env):
    env.login_owner(); env.store["shopee_orders_df"] = "x"
    auth_guard.logout(clear_all=True)
    assert env.store == {} and auth_guard.current_user() is None


@with_env
def test_require_role_matrix(env):
    raises(auth_guard.NotAuthenticated, auth_guard.require_role, "OWNER")
    env.login_packer()
    assert auth_guard.require_role()["role"] == "PACKER"
    assert auth_guard.require_role("OWNER", "PACKER")["role"] == "PACKER"
    raises(auth_guard.PermissionDenied, auth_guard.require_role, "OWNER")
    raises(ValueError, auth_guard.require_role, "owner")
    assert not auth_guard.is_owner()
    env.store.clear(); env.login_owner()
    assert auth_guard.is_owner()


# ======================================================================
# G. Account administration (OWNER only, server-side)
# ======================================================================
ADMIN_FUNCS = lambda: [
    lambda: account_admin.list_packers(),
    lambda: account_admin.create_packer(),
    lambda: account_admin.reset_packer_pin(PACKER_UUID),
    lambda: account_admin.set_packer_active(PACKER_UUID, False),
    lambda: account_admin.unlock_packer(PACKER_UUID),
    lambda: account_admin.unlock_all_packers(confirm=True),
]


@with_env
def test_packer_and_anonymous_cannot_use_any_admin_function(env):
    for c in ADMIN_FUNCS():
        raises(auth_guard.NotAuthenticated, c)
    env.login_packer()
    for c in ADMIN_FUNCS():
        raises(auth_guard.PermissionDenied, c)
    assert [n for n, _ in env.db.rpc_log] == ["packer_login"]                # only the login itself; no admin RPC
    assert [x for x in env.db.log if x[1] == "packer_accounts"] == []


@with_env
def test_create_packer_assigns_next_id_and_returns_pin_once(env):
    env.login_owner()
    a = account_admin.create_packer()
    b = account_admin.create_packer()
    assert (a["user_code"], b["user_code"]) == ("09", "10")
    for out in (a, b):
        assert re.fullmatch(r"\d{6}", out["pin"]) and not account_admin._is_weak_pin(out["pin"])
    assert [n for n, _ in env.db.rpc_log] == ["packer_reserve_code", "packer_create"] * 2
    for n, params in env.db.rpc_log:
        if n == "packer_create":
            assert HEX64.fullmatch(params["p_secret"]) and set(params) == {"p_code", "p_secret"}
            assert a["pin"] != params["p_secret"] and b["pin"] != params["p_secret"]
    # the new PIN really works through the normal login path, and only for its own ID
    env.store.clear()
    assert auth_guard.authenticate_pin("09", a["pin"])["label"] == "ID 09"
    raises(auth_guard.AuthError, auth_guard.authenticate_pin, "10", a["pin"])
    assert a["pin"] not in "\n".join(env.logs.lines) + json.dumps(env.store) + json.dumps(env.db.rpc_log)


@with_env
def test_failed_create_burns_the_id_and_never_reuses_it(env):
    env.login_owner()
    env.db.rpc_fail = lambda n: n == "packer_create"
    e = raises(auth_guard.AuthError, account_admin.create_packer)
    assert "09" in str(e) and "tidak akan dipakai ulang" in str(e) and not HEX64.search(str(e))
    env.db.rpc_fail = None
    assert account_admin.create_packer()["user_code"] == "10"


@with_env
def test_create_rejects_bad_reserve_or_create_responses(env):
    env.login_owner()
    for bad in (None, "", "7", "00", "100", 7, {"x": 1}):
        env.db.rpc_override["packer_reserve_code"] = bad
        raises(auth_guard.AuthError, account_admin.create_packer)
    env.db.rpc_override.clear()
    for bad in (None, {}, {"user_id": "u", "user_code": "99"}, {"user_code": "09"}):
        env.db.rpc_override["packer_create"] = bad
        raises(auth_guard.AuthError, account_admin.create_packer)


def test_generate_pin_properties():
    pins = [account_admin.generate_pin() for _ in range(3000)]
    assert all(re.fullmatch(r"\d{6}", p) for p in pins)
    assert not any(account_admin._is_weak_pin(p) for p in pins)
    assert len(set(pins)) > 2900
    assert len({p[0] for p in pins}) == 10 and len({p[5] for p in pins}) == 10
    for weak in ("000000", "111111", "123456", "654321", "234567", "123123", "121212", "987654"):
        assert account_admin._is_weak_pin(weak), weak
    for ok in ("482913", "135792", "908172"):
        assert not account_admin._is_weak_pin(ok), ok


def test_generate_pin_uses_the_csprng():
    calls = []
    orig = account_admin.secrets.randbelow
    account_admin.secrets.randbelow = lambda n: (calls.append(n), 482913)[1]
    try:
        assert account_admin.generate_pin() == "482913" and calls == [10 ** 6]
    finally:
        account_admin.secrets.randbelow = orig


@with_env
def test_reset_pin_replaces_credential_kills_sessions_and_returns_new_pin(env):
    env.login_owner()
    out = account_admin.reset_packer_pin(PACKER_UUID)
    assert out["user_code"] == "07" and re.fullmatch(r"\d{6}", out["pin"])
    name, params = env.db.rpc_log[-1]
    assert name == "packer_set_pin" and params["p_user_id"] == PACKER_UUID and HEX64.fullmatch(params["p_secret"])
    env.store.clear()
    raises(auth_guard.AuthError, env.login_packer)                              # old PIN dead
    assert auth_guard.authenticate_pin("07", out["pin"])["epoch"] == 4          # epoch bumped (3 -> 4)


@with_env
def test_disable_enable_unlock_call_the_right_functions(env):
    env.login_owner()
    account_admin.set_packer_active(PACKER_UUID, False)
    account_admin.set_packer_active(PACKER_UUID, True)
    account_admin.unlock_packer(PACKER_UUID)
    assert env.db.rpc_log == [("packer_set_active", {"p_user_id": PACKER_UUID, "p_active": False}),
                              ("packer_set_active", {"p_user_id": PACKER_UUID, "p_active": True}),
                              ("packer_unlock", {"p_user_id": PACKER_UUID})]


@with_env
def test_unlock_all_requires_explicit_confirmation_and_calls_one_rpc(env):
    env.login_owner()
    for bad in (False, None, 1, "yes", "True", 0, [True]):
        raises(auth_guard.AuthError, account_admin.unlock_all_packers, bad)
    raises(auth_guard.AuthError, account_admin.unlock_all_packers)
    assert env.db.rpc_log == []
    env.db.tables["packer_accounts"][0].update(locked_until="2099-01-01T00:00:00+00:00", failed_streak=6, window_failures=15)
    assert account_admin.unlock_all_packers(confirm=True) == 1
    assert env.db.rpc_log == [("packer_unlock_all", {})]
    a = env.db.tables["packer_accounts"][0]
    assert (a["locked_until"], a["failed_streak"], a["window_failures"]) == (None, 0, 0)


@with_env
def test_unlock_all_rejects_bad_responses_and_scrubs_errors(env):
    env.login_owner()
    for bad in (None, "3", True, -1, {"n": 1}, 1.5):
        env.db.rpc_override["packer_unlock_all"] = bad
        raises(auth_guard.AuthError, account_admin.unlock_all_packers, True)
    env.db.rpc_override.clear()
    env.db.rpc_fail = lambda n: n == "packer_unlock_all"
    e = raises(auth_guard.AuthError, account_admin.unlock_all_packers, True)
    assert not HEX64.search(str(e))


@with_env
def test_admin_targets_are_validated_before_any_rpc(env):
    env.login_owner()
    for bad in ("not-a-uuid", "", None, "00000000-0000-0000-0000-000000000099"):
        for fn in (account_admin.reset_packer_pin, account_admin.unlock_packer,
                   lambda u: account_admin.set_packer_active(u, False)):
            raises(auth_guard.AuthError, fn, bad)
    assert env.db.rpc_log == []


@with_env
def test_admin_errors_are_scrubbed_of_secrets(env):
    env.login_owner()
    env.db.rpc_fail = lambda n: n == "packer_set_pin"
    e = raises(auth_guard.AuthError, account_admin.reset_packer_pin, PACKER_UUID)
    assert "[redacted]" in str(e) and not HEX64.search(str(e))
    assert not HEX64.search("\n".join(env.logs.lines))


@with_env
def test_app_never_selects_pin_hash_or_star(env):
    env.login_owner()
    account_admin.list_packers()
    env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
    auth_guard.current_user()
    env.store.clear(); env.login_packer()
    env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
    auth_guard.current_user()                      # FakeDB.Query.select asserts on every packer_accounts select


@with_env
def test_owner_demoted_mid_session_loses_admin_access(env):
    env.login_owner()
    env.db.tables["profiles"][0]["role"] = "PACKER"
    env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
    raises(auth_guard.NotAuthenticated, account_admin.create_packer)
    assert env.db.rpc_log == []


def test_prod_modules_never_select_pin_hash_statically():
    import ast
    for path in ("auth_guard.py", "account_admin.py", "app.py"):
        for node in ast.walk(ast.parse(open(os.path.join(REPO, path)).read())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "select":
                for a in node.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        assert "pin_hash" not in a.value and a.value.strip() != "*", (path, a.value)


# ======================================================================
# H. Streamlit wiring (AppTest, real app.py / pages / fragment guard)
# ======================================================================
try:
    from streamlit.testing.v1 import AppTest
    import pandas as pd
    HAVE_APPTEST = True
except ImportError:
    HAVE_APPTEST = False

LOGIN_LABELS = ["ID (2 digit)", "PIN (6 digit)", "Email", "Password"]


def _patch_shopee_for_app(env):
    import shopee_auth
    shopee_auth._get_supabase_client = lambda: env.db


def _restore_shopee():
    import importlib
    import shopee_auth
    importlib.reload(shopee_auth)


def _orders_df():
    return pd.DataFrame([{
        "No. Pesanan": "ORD-ONBIE-1", "No. Resi": "RESI1", "Username (Pembeli)": "buyer",
        "Nama Penerima": "B**", "Kota/Kabupaten": "KOTA X", "Provinsi": "BENGKULU",
        "SKU Induk": "SKU1", "Nama Produk": "Sandal", "Nama Barang": "Sandal",
        "Nama Variasi": "Hitam 40", "Jumlah": 1, "Berat (Kg)": 0.5,
        "Status Pesanan": "Perlu Dikirim", "Waktu Pesanan Dibuat": "2026-10-24 08:00:00",
        "Tenggat Pengiriman": "2026-10-26 08:00:00", "Antar ke counter/ pick-up": "SPX",
        "Ekspedisi": "SPX", "Catatan dari Pembeli": "-", "Platform": "Shopee",
        "Toko": "Onbie", "Sumber": "Shopee API", "Shop ID": ONBIE,
    }])


def _app(path="app.py"):
    at = AppTest.from_file(os.path.join(REPO, path), default_timeout=60)
    # app.py's existing secrets bridge only tolerates a missing KEY, not a missing
    # secrets FILE (production always has one) - give AppTest a non-empty store.
    at.secrets["_test_placeholder"] = "x"
    return at


def _login_packer(at, code=PACKER_CODE, pin=PACKER_PIN):
    at.text_input[0].input(code); at.text_input[1].input(pin); at.button[0].click(); at.run()


def _login_owner(at):
    at.text_input[2].input(OWNER_EMAIL); at.text_input[3].input(OWNER_PW); at.button[1].click(); at.run()


def _labels(at):
    return [t.label for t in at.text_input]


def _sidebar_keys(at):
    return {b.key for b in at.sidebar.button}


def _no_exception(at):
    assert not at.exception, [e.value for e in at.exception]


def apptest(fn):
    def wrapper():
        if not HAVE_APPTEST:
            print("  (skipped: streamlit not installed)"); return
        env = Env(use_real_session=True)
        _patch_shopee_for_app(env)
        try:
            fn(env)
        finally:
            env.close()
            _restore_shopee()
    wrapper.__name__ = fn.__name__
    return wrapper


@apptest
def test_app_anonymous_sees_only_login_and_nothing_else_runs(env):
    import shopee_auth
    touched = []
    orig = shopee_auth.load_tokens
    shopee_auth.load_tokens = lambda *a, **k: touched.append("load_tokens") or orig(*a, **k)
    try:
        at = _app(); at.run()
    finally:
        shopee_auth.load_tokens = orig
    _no_exception(at)
    assert _labels(at) == LOGIN_LABELS
    assert [t.label for t in at.tabs] == ["PACKER (ID + PIN)", "OWNER (email)"]
    assert not at.sidebar.button and not at.sidebar.expander and not list(at.sidebar.header)
    assert touched == [] and env.db.log == [] and env.db.rpc_log == []


@apptest
def test_app_fails_closed_when_auth_not_configured(env):
    for missing in ("SUPABASE_ANON_KEY", "PIN_PEPPER"):
        saved = os.environ.pop(missing)
        at = _app(); at.run()
        _no_exception(at)
        assert any(missing in e.value for e in at.error) and not at.text_input and not at.sidebar.button
        os.environ[missing] = saved


@apptest
def test_app_wrong_credentials_stay_on_login(env):
    at = _app(); at.run()
    at.text_input[0].input("07"); at.text_input[1].input("000001"); at.button[0].click(); at.run()
    assert any("ID atau PIN salah" in e.value for e in at.error) and _labels(at) == LOGIN_LABELS
    at.text_input[2].input(OWNER_EMAIL); at.text_input[3].input("WrongPass123"); at.button[1].click(); at.run()
    assert any("Email atau password salah" in e.value for e in at.error) and _labels(at) == LOGIN_LABELS


@apptest
def test_app_packer_ui_has_no_owner_controls(env):
    at = _app(); at.run(); _login_packer(at)
    _no_exception(at)
    keys = _sidebar_keys(at)
    assert "btn_logout" in keys
    assert not any(k.startswith(("btn_connect_shopee_", "btn_reconnect_shopee_", "btn_create_packer")) for k in keys), keys
    assert not {e.label for e in at.sidebar.expander}
    assert any("ID 07" in c.value and "PACKER" in c.value for c in at.sidebar.caption)


@apptest
def test_app_owner_ui_has_owner_controls_and_lists_packers(env):
    at = _app(); at.run(); _login_owner(at)
    _no_exception(at)
    keys, exps = _sidebar_keys(at), {e.label for e in at.sidebar.expander}
    assert {"btn_connect_shopee_1272241861", "btn_connect_shopee_1179306776", "btn_logout", "btn_create_packer"} <= keys, keys
    assert "👥 Kelola Akun PACKER" in exps and "🔒 Temporary Production Data Export" in exps
    assert {"btn_disable_" + PACKER_UUID, "btn_resetpin_" + PACKER_UUID, "btn_enable_" + INACTIVE_UUID} <= keys


EVIL_HTML = "<img src=x onerror=alert(1)>"


@apptest
def test_app_rendered_markdown_has_no_raw_injected_tags(env):
    """Auth-dependent render check for the XSS change (the pure unit tests are in
    test_html_escape.py): hostile order data, real app.py behind a real PACKER login."""
    at = _app(); at.run(); _login_packer(at)
    df = _orders_df()
    df["Nama Produk"] = EVIL_HTML; df["Nama Variasi"] = '"><script>alert(2)</script>'
    df["SKU Induk"] = EVIL_HTML; df["Toko"] = EVIL_HTML
    at.session_state["shopee_orders_df"] = df; at.run()
    at.main.text_input[0].input("ORD-ONBIE-1")
    [b for b in at.main.button if "Cari" in b.label][0].click(); at.run()
    _no_exception(at)
    # Only st.markdown(..., unsafe_allow_html=True) renders raw HTML; plain st.markdown
    # (e.g. the "Detail Order" lines) is sanitised by Streamlit itself.
    html_md = [m for m in at.main.markdown if getattr(m, "allow_html", False)]
    assert html_md, "expected unsafe_allow_html markdown elements"
    md = "\n".join(m.value for m in html_md)
    assert not re.search(r"<(img|script)[^>]*>", md), md[:400]
    assert "&lt;img src=x onerror=alert(1)&gt;" in md


def _lock_everyone(env):
    for a in env.db.tables["packer_accounts"]:
        a.update(locked_until="2099-01-01T00:00:00+00:00", failed_streak=5, window_failures=15)


def _unlock_all_calls(env):
    return [n for n, _ in env.db.rpc_log].count("packer_unlock_all")


@apptest
def test_app_unlock_all_needs_confirmation_and_cancel_does_nothing(env):
    _lock_everyone(env)
    at = _app(); at.run(); _login_owner(at)
    assert "btn_unlock_all" in _sidebar_keys(at) and "btn_unlock_all_confirm" not in _sidebar_keys(at)
    at.sidebar.button(key="btn_unlock_all").click(); at.run()
    _no_exception(at)
    assert _unlock_all_calls(env) == 0                                          # first click executes NOTHING
    assert {"btn_unlock_all_confirm", "btn_unlock_all_cancel"} <= _sidebar_keys(at) and "btn_unlock_all" not in _sidebar_keys(at)
    assert any("SEMUA akun PACKER" in w.value for w in at.sidebar.warning)
    at.sidebar.button(key="btn_unlock_all_cancel").click(); at.run()
    assert "btn_unlock_all" in _sidebar_keys(at) and "btn_unlock_all_confirm" not in _sidebar_keys(at)
    assert _unlock_all_calls(env) == 0
    assert all(a["locked_until"] for a in env.db.tables["packer_accounts"])


@apptest
def test_app_unlock_all_confirmed_runs_once_and_reports(env):
    _lock_everyone(env)
    at = _app(); at.run(); _login_owner(at)
    at.sidebar.button(key="btn_unlock_all").click(); at.run()
    at.sidebar.button(key="btn_unlock_all_confirm").click(); at.run()
    _no_exception(at)
    assert _unlock_all_calls(env) == 1
    assert all(a["locked_until"] is None and a["window_failures"] == 0 for a in env.db.tables["packer_accounts"])
    assert any("2 akun" in x.value for x in at.sidebar.success)
    assert "btn_unlock_all" in _sidebar_keys(at) and "btn_unlock_all_confirm" not in _sidebar_keys(at)   # back to the first step
    at.run()
    assert not any("dibuka" in x.value for x in at.sidebar.success)             # message is shown once
    assert _unlock_all_calls(env) == 1


@apptest
def test_app_packer_never_sees_unlock_all(env):
    at = _app(); at.run(); _login_packer(at)
    keys = _sidebar_keys(at)
    assert not any(k.startswith("btn_unlock") for k in keys), keys
    assert _unlock_all_calls(env) == 0


@apptest
def test_app_pending_unlock_all_confirmation_does_not_survive_losing_owner_rights(env):
    _lock_everyone(env)
    at = _app(); at.run(); _login_owner(at)
    at.sidebar.button(key="btn_unlock_all").click(); at.run()                   # confirmation pending
    env.db.tables["profiles"][0]["role"] = "PACKER"                              # OWNER demoted meanwhile
    env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
    at.run()
    assert _labels(at) == LOGIN_LABELS
    assert _unlock_all_calls(env) == 0


@apptest
def test_app_owner_creates_packer_pin_is_shown_once(env):
    at = _app(); at.run(); _login_owner(at)
    at.sidebar.button(key="btn_create_packer").click(); at.run()
    _no_exception(at)
    pins = [c.value for c in at.sidebar.code]
    assert len(pins) == 1 and re.fullmatch(r"\d{6}", pins[0]), pins
    assert any(a["user_code"] == "09" for a in env.db.tables["packer_accounts"])
    at.run()                                                         # any later rerun: the PIN is gone
    assert [c.value for c in at.sidebar.code] == []
    assert pins[0] not in json.dumps(env.db.rpc_log) and pins[0] not in "\n".join(env.logs.lines)


@apptest
def test_app_shopee_oauth_callback_is_owner_only(env):
    import shopee_auth
    handled = []
    shopee_auth.handle_oauth_callback = lambda code, shop_id: (handled.append((code, shop_id)) or {
        "access_token": "a" * 12, "refresh_token": "r" * 12, "expire_in": 14400, "fetch_time": 1, "shop_id": shop_id})

    at = _app(); at.query_params["code"] = "abc"; at.query_params["shop_id"] = str(ONBIE); at.run()
    assert handled == [] and _labels(at) == LOGIN_LABELS                 # anonymous: code survives, not consumed

    _login_packer(at)                                                    # PACKER: refused, code dropped
    assert handled == [] and any("ditolak" in e.value.lower() for e in at.error)
    assert "code" not in at.query_params

    at2 = _app(); at2.query_params["code"] = "abc"; at2.query_params["shop_id"] = str(ONBIE); at2.run()
    _login_owner(at2)                                                    # OWNER: handled exactly once
    assert handled == [("abc", ONBIE)], handled


@apptest
def test_app_pack_flow_as_packer_writes_history(env):
    at = _app(); at.run(); _login_packer(at)
    at.session_state["shopee_orders_df"] = _orders_df(); at.run(); _no_exception(at)

    def submit(q):
        at.main.text_input[0].input(q)
        [b for b in at.main.button if "Cari" in b.label][0].click(); at.run()

    submit("ORD-ONBIE-1"); _no_exception(at)
    submit(""); _no_exception(at)
    assert [(r["shop_id"], r["order_number"]) for r in env.db.tables["packed_orders"]] == [(ONBIE, "ORD-ONBIE-1")]
    assert len(env.db.tables["packed_snapshot_items"]) == 1


@apptest
def test_app_disabled_packer_is_bounced_to_login_mid_session(env):
    at = _app(); at.run(); _login_packer(at)
    assert "Email" not in _labels(at)
    env.db.tables["packer_accounts"][0]["is_active"] = False
    env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
    at.run()
    assert _labels(at) == LOGIN_LABELS and not at.sidebar.button


@apptest
def test_app_pack_is_refused_without_valid_session_even_if_ui_state_remains(env):
    at = _app(); at.run(); _login_packer(at)
    at.session_state["shopee_orders_df"] = _orders_df(); at.run()
    at.main.text_input[0].input("ORD-ONBIE-1"); [b for b in at.main.button if "Cari" in b.label][0].click(); at.run()
    env.db.tables["packer_accounts"][0]["session_epoch"] += 1             # PIN reset by the OWNER right now
    env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
    at.main.text_input[0].input(""); [b for b in at.main.button if "Cari" in b.label][0].click(); at.run()
    assert env.db.tables["packed_orders"] == [] and env.db.tables["packed_snapshot_items"] == []
    assert _labels(at) == LOGIN_LABELS


class _RerunCalled(BaseException):
    """Stand-in for Streamlit's RerunException (also a BaseException)."""


def _with_fake_rerun(fn):
    import streamlit as st
    orig = st.rerun
    calls = []
    st.rerun = lambda *a, **k: (calls.append(1), (_ for _ in ()).throw(_RerunCalled()))[0]
    try:
        return fn(calls)
    finally:
        st.rerun = orig


@with_env
def test_fragment_guard_reruns_app_when_session_gone_or_unverifiable(env):
    try:
        import streamlit  # noqa: F401
    except ImportError:
        print("  (skipped: streamlit not installed)"); return

    def body(calls):
        raises(_RerunCalled, auth_guard.fragment_guard)                    # never logged in
        env.login_packer()
        assert auth_guard.fragment_guard(touch=True)["role"] == "PACKER"
        assert len(calls) == 1
        env.db.tables["packer_accounts"][0]["is_active"] = False
        env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
        raises(_RerunCalled, auth_guard.fragment_guard)                    # disabled mid-session
        env.store.clear(); env.db.tables["packer_accounts"][0]["is_active"] = True
        env.login_packer()
        env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
        env.db.fail = lambda op, t: t == "packer_accounts"                 # cannot verify -> fail closed
        raises(_RerunCalled, auth_guard.fragment_guard)
        env.db.fail = None
        env.now[0] += auth_guard.IDLE_TIMEOUT_SECONDS + 1                  # idle timeout
        raises(_RerunCalled, auth_guard.fragment_guard)
        assert len(calls) == 4

    _with_fake_rerun(body)


# save_packed_order / persist_packed_order: REAL functions from app.py, real
# auth_guard, fake Supabase. Proves the check lives inside the write path,
# independent of fragment_guard or any UI state.
def _build_app_pack_namespace(env):
    import ast
    src = open(os.path.join(REPO, "app.py")).read()
    wanted_defs = {"PackingHistoryError", "_history_client", "_read_identity",
                   "_build_snapshot_items", "persist_packed_order", "save_packed_order"}
    wanted_consts = {"PACKED_ORDERS_TABLE", "SNAPSHOT_ITEMS_TABLE", "SNAPSHOT_DB_TO_APP"}
    parts = []
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in wanted_defs:
            parts.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) in wanted_consts:
            parts.append(ast.get_source_segment(src, node))
    assert len(parts) == len(wanted_defs) + len(wanted_consts), "app.py structure changed"

    class FakeSt:
        def __init__(self): self.errors = []
        def error(self, msg): self.errors.append(msg)

    class Clearable:
        def __init__(self): self.cleared = 0
        def clear(self): self.cleared += 1

    from datetime import datetime
    from zoneinfo import ZoneInfo
    ns = {
        "pd": pd, "st": FakeSt(), "logger": logging.getLogger("t"), "auth_guard": auth_guard,
        "_shopee_auth": types.SimpleNamespace(get_supabase_client=lambda: env.db),
        "_now_wib": lambda: datetime(2026, 10, 24, 9, 0, 0, tzinfo=ZoneInfo("Asia/Jakarta")),
        "load_packed_df": Clearable(), "load_snapshots_df": Clearable(),
    }
    exec("\n\n".join(parts), ns)
    return ns


def _pack_rows():
    return pd.DataFrame([{"No. Pesanan": "ORD1", "Username (Pembeli)": "u", "Nama Penerima": "n",
                          "Platform": "Shopee", "Toko": "Onbie", "Provinsi": "p", "Kota/Kabupaten": "k",
                          "Antar ke counter/ pick-up": "SPX", "Ekspedisi": "SPX",
                          "Nama Variasi": "v", "Jumlah": 1}])


@with_env
def test_save_packed_order_enforces_role_inside_the_write_path(env):
    try:
        import pandas  # noqa: F401
    except ImportError:
        print("  (skipped: pandas not installed)"); return
    ns = _build_app_pack_namespace(env)
    save, st = ns["save_packed_order"], ns["st"]
    writes = lambda: [x for x in env.db.log if x[0] == "upsert"]

    assert save(ONBIE, "ORD1", _pack_rows()) is False                      # anonymous
    assert any("PACKING DITOLAK" in e for e in st.errors) and writes() == [] and ns["load_packed_df"].cleared == 0
    raises(auth_guard.NotAuthenticated, ns["persist_packed_order"], ONBIE, "ORD1", _pack_rows(), ns["_now_wib"]())
    assert writes() == []

    for who, order in (("packer", "ORD1"), ("owner", "ORD2")):             # both roles may pack
        env.store.clear(); env.login(who)
        assert save(ONBIE, order, _pack_rows()) is True
    assert {r["order_number"] for r in env.db.tables["packed_orders"]} == {"ORD1", "ORD2"}

    env.store.clear(); env.login_packer()                                   # disabled after login
    env.db.tables["packer_accounts"][0]["is_active"] = False
    env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
    n_before = len(env.db.tables["packed_orders"]); st.errors.clear()
    assert save(GOTTO, "ORD3", _pack_rows()) is False
    assert len(env.db.tables["packed_orders"]) == n_before and any("DITOLAK" in e for e in st.errors)

    env.db.tables["packer_accounts"][0]["is_active"] = True                 # unverifiable -> refused
    env.store.clear(); env.login_packer()
    env.now[0] += auth_guard.REVALIDATE_SECONDS + 1
    env.db.fail = lambda op, t: t == "packer_accounts"
    assert save(GOTTO, "ORD4", _pack_rows()) is False
    env.db.fail = None
    assert len(env.db.tables["packed_orders"]) == n_before


TIKTOK_SCRIPT_WRAPPER = """
import os, sys
sys.path.insert(0, {repo!r})
import tiktok_auth
CALLS = sys.modules.setdefault("_tiktok_calls", __import__("types").SimpleNamespace(n=[]))
tiktok_auth.handle_oauth_callback = lambda code: (CALLS.n.append(code) or {{
    "seller_name": "S", "seller_base_region": "ID", "shops": [], "granted_scopes": [], "missing_scopes": []}})
exec(compile(open({page!r}).read(), {page!r}, "exec"))
"""


@apptest
def test_tiktok_callback_page_is_owner_only(env):
    page = os.path.join(REPO, "pages", "tiktok_callback.py")
    calls = sys.modules.setdefault("_tiktok_calls", types.SimpleNamespace(n=[]))
    calls.n.clear()
    script = TIKTOK_SCRIPT_WRAPPER.format(repo=REPO, page=page)

    at = AppTest.from_string(script, default_timeout=30); at.query_params["code"] = "tt-code"; at.run()
    assert calls.n == [] and _labels(at) == LOGIN_LABELS

    _login_packer(at)
    assert calls.n == [] and any("ditolak" in e.value.lower() for e in at.error)
    assert "code" not in at.query_params

    at2 = AppTest.from_string(script, default_timeout=30); at2.query_params["code"] = "tt-code"; at2.run()
    _login_owner(at2)
    assert calls.n == ["tt-code"], calls.n


# ======================================================================
# I. Structural guards (cheap, catch future regressions: new fragments / pages)
# ======================================================================
def _parse(path):
    import ast
    return ast.parse(open(os.path.join(REPO, path)).read())


def _calls(node, dotted):
    import ast
    return any(isinstance(n, ast.Call) and ast.unparse(n.func) == dotted for n in ast.walk(node))


def test_every_st_fragment_starts_with_fragment_guard():
    import ast
    found = 0
    for path in ["app.py"] + [os.path.join("pages", f) for f in sorted(os.listdir(os.path.join(REPO, "pages"))) if f.endswith(".py")]:
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.FunctionDef) and any("fragment" in ast.unparse(d) for d in node.decorator_list):
                found += 1
                body = [b for b in node.body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
                first = ast.unparse(body[0])
                assert first.startswith("auth_guard.fragment_guard("), f"{path}:{node.name} must start with auth_guard.fragment_guard(...), starts with {first!r}"
    assert found >= 2, "expected the scan and auto-sync fragments"


def test_every_page_script_calls_gate():
    for f in sorted(os.listdir(os.path.join(REPO, "pages"))):
        if f.endswith(".py"):
            assert _calls(_parse(os.path.join("pages", f)), "auth_guard.gate"), f"pages/{f} has no auth_guard.gate() call"


def test_app_gate_runs_before_any_privileged_toplevel_code():
    import ast
    tree = _parse("app.py")
    order = []
    for i, node in enumerate(tree.body):
        text = ast.unparse(node)
        if text.startswith("_auth_user = auth_guard.gate("):
            order.append(("gate", i))
        elif text in ("_bootstrap_shopee_tokens_from_secrets()", "_handle_shopee_oauth()"):
            order.append((text, i))
        elif isinstance(node, ast.With) and "st.sidebar" in ast.unparse(node.items[0]):
            order.append(("sidebar", i))
    gate_i = [i for n, i in order if n == "gate"]
    assert len(gate_i) == 1, order
    assert set(n for n, _ in order if n != "gate") == {"_bootstrap_shopee_tokens_from_secrets()", "_handle_shopee_oauth()", "sidebar"}, order
    assert all(i > gate_i[0] for n, i in order if n != "gate"), order


# ======================================================================
# Runner
# ======================================================================
def _collect():
    return [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]


if __name__ == "__main__":
    failed = []
    tests = _collect()
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            import traceback
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=4)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
