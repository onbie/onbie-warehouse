"""
auth_guard.py
=============
Login + role authorization for the Onbie Packing System (OWNER / PACKER).

Two login paths, one session model
----------------------------------
* OWNER  = Supabase Auth (email + password). Passwords are only ever sent to
  Supabase Auth; this app never stores, logs or caches them. Sign-in uses a
  THROWAWAY client built from SUPABASE_ANON_KEY, never the shared data
  client from shopee_auth.get_supabase_client(): supabase-py swaps a
  client's Authorization header to the signed-in user's JWT on sign-in,
  which on the shared, process-wide data client would silently change the
  credentials used by every other session. Authorization = `profiles`.
* PACKER = 2-digit ID + 6-digit PIN (IDs assigned automatically, never
  reused). PACKERs are NOT Supabase Auth users (no synthetic emails): the
  app never uses a Supabase session for them, so Supabase Auth would add
  nothing but an extra internet-facing endpoint. Instead the app sends
  HMAC-SHA256(PIN_PEPPER, "id:pin") to the Postgres function
  public.packer_login(), which verifies it against a bcrypt hash AND does
  the lockout bookkeeping atomically under a row lock (durable, shared by
  every process, restart-proof). The raw PIN is never stored, logged or sent
  to Supabase; the app role cannot even read pin_hash (see
  supabase_auth_setup.sql).
* Authorization for both = rows read server-side with the service-role data
  client. Role is NEVER taken from anything the user can edit or from the
  browser.
* The session keeps only {user_id, role, kind, label, epoch, timestamps} in
  st.session_state (server memory). No token, password or PIN is kept.
* There is no "auth disabled" mode and no fake login: if the auth
  configuration is incomplete, the app shows an error and stops (fail
  closed).

Enforcement points (see app.py / pages/tiktok_callback.py)
-----------------------------------------------------------
* gate()            - top of every page script: login form or st.stop().
* fragment_guard()  - first line of every @st.fragment body. A fragment
                      rerun does NOT re-execute the page-level gate, so each
                      fragment re-checks the session itself.
* require_role()    - inside every privileged function (pack, OAuth, sync,
                      account management), so authorization does not depend
                      on which UI element happened to be rendered.

Everything here except the small Streamlit UI helpers is Streamlit-free and
unit-testable with fake clients (see test_auth.py).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

ROLE_OWNER = "OWNER"
ROLE_PACKER = "PACKER"
VALID_ROLES = (ROLE_OWNER, ROLE_PACKER)

PROFILES_TABLE = "profiles"          # OWNER accounts (Supabase Auth users)
PACKER_TABLE = "packer_accounts"     # PACKER accounts (ID + PIN)
SESSION_KEY = "_auth_user"

KIND_OWNER = "owner"
KIND_PACKER = "packer"
_KIND_ROLE = {KIND_OWNER: ROLE_OWNER, KIND_PACKER: ROLE_PACKER}

# Secrets/env names. SUPABASE_URL / SUPABASE_KEY already exist (data client,
# service-role). SUPABASE_ANON_KEY is used ONLY for OWNER sign-in; PIN_PEPPER
# only for deriving the PACKER PIN secret. Neither value is ever logged.
REQUIRED_ENV = ("SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_ANON_KEY", "PIN_PEPPER")
MIN_PEPPER_LENGTH = 32

# Session policy
REVALIDATE_SECONDS = 60            # re-read profiles (role / is_active) this often
IDLE_TIMEOUT_SECONDS = 4 * 3600    # no user activity -> login again
MAX_SESSION_SECONDS = 12 * 3600    # absolute session lifetime

# In-process brute-force throttle for the OWNER email/password path ONLY.
# It is best-effort (lost on restart, per process). The PACKER path does NOT
# use it: PACKER lockout is durable and atomic inside Postgres.
MAX_FAILED_ATTEMPTS = 5
FAILURE_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 5 * 60

_CODE_RE = re.compile(r"^[0-9]{2}$")
_PIN_RE = re.compile(r"^[0-9]{6}$")
_HEX64_RE = re.compile(r"[0-9a-fA-F]{64}")

_clock = time.time  # replaced in tests


class AuthError(Exception):
    """Base class. str(e) is always safe to show to the user."""


class AuthConfigError(AuthError):
    """Auth configuration incomplete/invalid (fail closed)."""


class NotAuthenticated(AuthError):
    """No valid login session."""


class PermissionDenied(AuthError):
    """Logged in, but the role is not allowed to do this."""


# --------------------------------------------------------------------------
# Session access (server-side st.session_state; tests monkeypatch _session)
# --------------------------------------------------------------------------
def _session():
    import streamlit as st
    return st.session_state


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def bridge_secrets():
    """st.secrets -> os.environ for the three Supabase values (same bridge
    pattern as app.py). Missing secrets (or no secrets file) are skipped;
    check_config() then fails closed and names what is missing."""
    import streamlit as st
    for name in REQUIRED_ENV:
        try:
            value = str(st.secrets[name]).strip()
        except Exception:
            continue
        if value:
            os.environ[name] = value


def _jwt_role(key):
    """Role claim of a legacy JWT-style Supabase key (anon / service_role),
    or None if the key is not a decodable JWT (e.g. new sb_* keys). The
    payload is only inspected, never trusted for authorization."""
    try:
        parts = key.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode())).get("role")
    except Exception:
        return None


def check_config():
    """Raise AuthConfigError (names only, never values) unless auth is fully
    and sanely configured."""
    env = {n: os.environ.get(n, "").strip() for n in REQUIRED_ENV}
    missing = [n for n, v in env.items() if not v]
    if missing:
        raise AuthConfigError(
            "Login belum bisa dipakai: secret berikut belum diset - " + ", ".join(missing) + "."
        )
    if env["SUPABASE_ANON_KEY"] == env["SUPABASE_KEY"]:
        raise AuthConfigError(
            "SUPABASE_ANON_KEY tidak boleh sama dengan SUPABASE_KEY (service-role)."
        )
    if _jwt_role(env["SUPABASE_ANON_KEY"]) == "service_role":
        raise AuthConfigError(
            "SUPABASE_ANON_KEY berisi key service-role. Pakai key anon/publishable untuk login."
        )
    if _jwt_role(env["SUPABASE_KEY"]) == "anon":
        raise AuthConfigError(
            "SUPABASE_KEY harus key service-role (server), bukan key anon."
        )
    if len(env["PIN_PEPPER"]) < MIN_PEPPER_LENGTH:
        raise AuthConfigError(f"PIN_PEPPER terlalu pendek (minimal {MIN_PEPPER_LENGTH} karakter acak).")
    if env["PIN_PEPPER"] in (env["SUPABASE_KEY"], env["SUPABASE_ANON_KEY"]):
        raise AuthConfigError("PIN_PEPPER tidak boleh sama dengan key Supabase.")


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------
def _new_auth_client():
    """A fresh, throwaway client for ONE sign-in (anon key, no session
    persistence, no refresh timer). Never cached, never shared."""
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL", "").strip()
    anon = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    try:
        from supabase import ClientOptions
        options = ClientOptions(auto_refresh_token=False, persist_session=False)
        return create_client(url, anon, options=options)
    except (ImportError, TypeError):
        return create_client(url, anon)


def _data_client():
    """The shared service-role client (profiles lookups, admin API). Never
    used for sign-in."""
    import shopee_auth
    client = shopee_auth.get_supabase_client()
    if client is None:
        raise AuthError("Supabase tidak tersedia atau belum dikonfigurasi.")
    return client


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def normalize_email(email):
    return str(email or "").strip().lower()


def scrub(text):
    """Remove anything that looks like a derived PIN secret (64 hex chars)
    from text that may be shown or logged."""
    return _HEX64_RE.sub("[redacted]", str(text))


def valid_code(code):
    return isinstance(code, str) and bool(_CODE_RE.match(code)) and code != "00"


def valid_pin(pin):
    return isinstance(pin, str) and bool(_PIN_RE.match(pin))


def derive_pin_secret(code, pin):
    """HMAC-SHA256(PIN_PEPPER, "onbie-pin-v1:<id>:<pin>") as 64 hex chars.
    This - never the raw PIN - is what leaves the app and what Postgres
    bcrypt-hashes. Without the pepper (server secret) the value cannot be
    produced or guessed, so a database dump or a direct call to any Supabase
    endpoint is useless for brute-forcing the 10^6 PIN space."""
    pepper = os.environ.get("PIN_PEPPER", "").strip()
    if len(pepper) < MIN_PEPPER_LENGTH:
        raise AuthConfigError("PIN_PEPPER belum diset dengan benar.")
    return hmac.new(
        pepper.encode(), f"onbie-pin-v1:{code}:{pin}".encode(), hashlib.sha256
    ).hexdigest()


def _call_rpc(name, params):
    """Call a Postgres function through the service-role data client. Never
    logs params (they contain the derived secret); errors are scrubbed."""
    client = _data_client()
    try:
        return client.rpc(name, params).execute().data
    except Exception as e:
        logger.warning("rpc %s failed: %s", name, type(e).__name__)
        raise AuthError("Layanan login tidak tersedia saat ini. Coba lagi nanti.") from None


# --------------------------------------------------------------------------
# Brute-force throttle (in-process, per email)
# --------------------------------------------------------------------------
_failures = {}
_failures_lock = threading.Lock()


def _throttle_check(email):
    now = _clock()
    with _failures_lock:
        stamps = [t for t in _failures.get(email, []) if now - t < FAILURE_WINDOW_SECONDS]
        if stamps:
            _failures[email] = stamps
        else:
            _failures.pop(email, None)
        if len(stamps) >= MAX_FAILED_ATTEMPTS and now - stamps[-1] < LOCKOUT_SECONDS:
            raise AuthError("Terlalu banyak percobaan login gagal. Coba lagi dalam beberapa menit.")


def _record_failure(email):
    with _failures_lock:
        _failures.setdefault(email, []).append(_clock())


def _clear_failures(email):
    with _failures_lock:
        _failures.pop(email, None)


def _reset_throttle_for_tests():
    with _failures_lock:
        _failures.clear()


# --------------------------------------------------------------------------
# Profiles (authorization source of truth)
# --------------------------------------------------------------------------
def _load_profile(user_id):
    """profiles row for this auth user id, or None if there is none. A
    database failure raises AuthError (fail closed - never 'no profile' and
    never 'allowed')."""
    client = _data_client()
    try:
        rows = (
            client.table(PROFILES_TABLE)
            .select("user_id,email,role,is_active")
            .eq("user_id", str(user_id))
            .execute().data
        ) or []
    except Exception as e:
        logger.warning("profiles lookup failed: %s", type(e).__name__)
        raise AuthError("Tidak dapat memverifikasi akun saat ini (database tidak tersedia).") from e
    return rows[0] if rows else None


def _profile_is_usable(profile):
    """OWNER profiles only: PACKERs authenticate with ID + PIN, never email."""
    return (
        isinstance(profile, dict)
        and profile.get("role") == ROLE_OWNER
        and profile.get("is_active") is True
    )


def _load_packer(user_id):
    """Status columns of a PACKER account (never pin_hash), or None. A
    database failure raises AuthError (fail closed)."""
    client = _data_client()
    try:
        rows = (
            client.table(PACKER_TABLE)
            .select("user_id,user_code,is_active,session_epoch")
            .eq("user_id", str(user_id))
            .execute().data
        ) or []
    except Exception as e:
        logger.warning("packer lookup failed: %s", type(e).__name__)
        raise AuthError("Tidak dapat memverifikasi akun saat ini (database tidak tersedia).") from e
    return rows[0] if rows else None


def _packer_is_usable(row, epoch):
    return (
        isinstance(row, dict)
        and row.get("is_active") is True
        and isinstance(row.get("session_epoch"), int)
        and row.get("session_epoch") == epoch
    )


def _start_session(user_id, kind, label, epoch=0):
    now = _clock()
    session = {
        "user_id": str(user_id),
        "role": _KIND_ROLE[kind],
        "kind": kind,
        "label": str(label),
        "epoch": int(epoch),
        "login_at": now,
        "last_active": now,
        "last_check": now,
    }
    _session()[SESSION_KEY] = session
    return dict(session)


# --------------------------------------------------------------------------
# Login / session
# --------------------------------------------------------------------------
_GENERIC_LOGIN_ERROR = "Email atau password salah."


def _is_credential_error(exc):
    name = type(exc).__name__
    status = getattr(exc, "status", None)
    if name == "AuthInvalidCredentialsError":
        return True
    return name == "AuthApiError" and status in (400, 401, 403, 422)


def authenticate(email, password):
    """OWNER login: verify email/password with Supabase Auth, then authorize
    against `profiles` (OWNER only). On success stores the (token-free)
    session and returns it. Raises AuthError with a user-safe message
    otherwise."""
    email = normalize_email(email)
    password = str(password or "")
    if not email or not password:
        raise AuthError("Email dan password wajib diisi.")
    check_config()
    _throttle_check(email)

    try:
        client = _new_auth_client()
        response = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as e:
        status = getattr(e, "status", None)
        if type(e).__name__ == "AuthApiError" and status == 429:
            raise AuthError("Terlalu banyak percobaan login. Coba lagi dalam beberapa menit.") from None
        if _is_credential_error(e):
            _record_failure(email)
            raise AuthError(_GENERIC_LOGIN_ERROR) from None
        # Outage/network/unknown: not the user's fault, not counted as a failed attempt.
        logger.warning("login service error: %s", type(e).__name__)
        raise AuthError("Layanan login tidak tersedia saat ini. Coba lagi nanti.") from None
    finally:
        password = None  # do not keep the plaintext around

    user = getattr(response, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        _record_failure(email)
        raise AuthError(_GENERIC_LOGIN_ERROR)

    profile = _load_profile(user_id)  # raises AuthError on DB failure (fail closed)
    if not _profile_is_usable(profile):
        raise AuthError("Akun ini tidak aktif atau bukan akun OWNER. PACKER login dengan ID dan PIN.")

    _clear_failures(email)
    return _start_session(user_id, KIND_OWNER, profile.get("email") or email)


_PIN_ERROR_FORMAT = "ID harus 2 digit dan PIN harus 6 digit."
_PIN_ERROR_INVALID = "ID atau PIN salah."


def authenticate_pin(code, pin):
    """PACKER login (2-digit ID + 6-digit PIN). The decision - including
    lockout counting - is made atomically by Postgres (packer_login); this
    function only derives the secret, calls it and maps the status. Any
    unexpected response is treated as a failure (fail closed)."""
    code = str(code or "").strip()
    pin = str(pin or "").strip()
    if not valid_code(code) or not valid_pin(pin):
        raise AuthError(_PIN_ERROR_FORMAT)
    check_config()
    secret = derive_pin_secret(code, pin)
    pin = None  # the raw PIN is not needed (or kept) beyond this point

    result = _call_rpc("packer_login", {"p_code": code, "p_secret": secret})
    status = result.get("status") if isinstance(result, dict) else None

    if status == "invalid":
        raise AuthError(_PIN_ERROR_INVALID)
    if status == "locked":
        wait = result.get("retry_after_seconds")
        minutes = max(1, -(-int(wait) // 60)) if isinstance(wait, int) and wait > 0 else 5
        if minutes >= 120:
            # 12 h lock (15 failures): say so, and that an OWNER can open it sooner.
            raise AuthError(
                f"Akun terkunci karena terlalu banyak percobaan gagal. Coba lagi dalam {-(-minutes // 60)} jam, "
                "atau hubungi OWNER untuk membukanya sekarang."
            )
        raise AuthError(f"Akun terkunci sementara. Coba lagi dalam {minutes} menit.")
    if status == "inactive":
        raise AuthError("Akun ini tidak aktif. Hubungi OWNER.")
    if (
        status == "ok"
        and isinstance(result.get("user_id"), str) and result["user_id"]
        and result.get("user_code") == code
        and isinstance(result.get("session_epoch"), int)
    ):
        return _start_session(result["user_id"], KIND_PACKER, f"ID {code}", result["session_epoch"])
    logger.warning("packer_login returned an unexpected response")
    raise AuthError("Layanan login tidak tersedia saat ini. Coba lagi nanti.")


def logout(clear_all=False):
    s = _session()
    if clear_all:
        s.clear()
    else:
        s.pop(SESSION_KEY, None)


def _session_is_well_formed(sess):
    return (
        isinstance(sess, dict)
        and sess.get("user_id")
        and sess.get("kind") in _KIND_ROLE
        and sess.get("role") == _KIND_ROLE[sess.get("kind")]   # a PACKER session can never claim OWNER
        and isinstance(sess.get("label"), str)
        and isinstance(sess.get("epoch"), int) and not isinstance(sess.get("epoch"), bool)
        and all(isinstance(sess.get(k), (int, float)) for k in ("login_at", "last_active", "last_check"))
    )


def current_user(touch=False):
    """The validated logged-in user (dict) or None.

    None = not logged in / expired / disabled / tampered (the stored session
    is dropped). Raises AuthError if the account status cannot be verified
    right now (database down): callers must treat that as 'not allowed',
    never as 'allowed'."""
    s = _session()
    sess = s.get(SESSION_KEY)
    if sess is None:
        return None
    if not _session_is_well_formed(sess):
        s.pop(SESSION_KEY, None)
        return None

    now = _clock()
    if now - sess["login_at"] > MAX_SESSION_SECONDS or now - sess["last_active"] > IDLE_TIMEOUT_SECONDS:
        s.pop(SESSION_KEY, None)
        return None

    if now - sess["last_check"] >= REVALIDATE_SECONDS:
        if sess["kind"] == KIND_OWNER:
            usable = _profile_is_usable(_load_profile(sess["user_id"]))
        else:
            # disable / PIN reset bump session_epoch -> every live session dies here
            usable = _packer_is_usable(_load_packer(sess["user_id"]), sess["epoch"])
        # (AuthError from the lookups propagates: fail closed, session kept)
        if not usable:
            s.pop(SESSION_KEY, None)
            return None
        sess = dict(sess, last_check=now)
        s[SESSION_KEY] = sess

    if touch:
        sess = dict(sess, last_active=now)
        s[SESSION_KEY] = sess
    return dict(sess)


def require_role(*roles, touch=True):
    """Server-side authorization check. Returns the user dict, or raises
    NotAuthenticated / PermissionDenied (both AuthError). Call this inside
    every privileged function. No roles given = any logged-in user."""
    for r in roles:
        if r not in VALID_ROLES:
            raise ValueError(f"unknown role: {r!r}")
    user = current_user(touch=touch)
    if user is None:
        raise NotAuthenticated("Sesi login tidak valid atau sudah berakhir. Silakan login ulang.")
    allowed = roles or VALID_ROLES
    if user["role"] not in allowed:
        raise PermissionDenied("Akses ditolak: fitur ini hanya untuk " + " / ".join(allowed) + ".")
    return user


def is_owner():
    """UI helper (hiding controls). NOT an authorization check - privileged
    functions call require_role() themselves."""
    try:
        user = current_user(touch=False)
    except AuthError:
        return False
    return bool(user) and user["role"] == ROLE_OWNER


# --------------------------------------------------------------------------
# Streamlit UI helpers
# --------------------------------------------------------------------------
def _render_login(st):
    st.subheader("🔐 Login")
    tab_packer, tab_owner = st.tabs(["PACKER (ID + PIN)", "OWNER (email)"])

    with tab_packer:
        with st.form("auth_pin_form"):
            code = st.text_input("ID (2 digit)", max_chars=2)
            pin = st.text_input("PIN (6 digit)", type="password", max_chars=6)
            submitted_pin = st.form_submit_button("Masuk")
        if submitted_pin:
            try:
                authenticate_pin(code, pin)
            except AuthError as e:
                st.error(f"❌ {e}")
            else:
                st.rerun()

    with tab_owner:
        with st.form("auth_login_form"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Masuk sebagai OWNER")
        if submitted:
            try:
                authenticate(email, password)
            except AuthError as e:
                st.error(f"❌ {e}")
            else:
                st.rerun()


def gate(roles=None):
    """Page-level gate: returns the user dict, or renders the login /
    error screen and calls st.stop(). `roles` restricts the page to those
    roles (default: any logged-in user). Call after bridge_secrets()."""
    import streamlit as st

    try:
        check_config()
    except AuthConfigError as e:
        st.error(f"❌ {e}")
        st.stop()

    try:
        user = current_user(touch=True)
    except AuthError as e:
        st.error(f"❌ {e}")
        st.button("🔄 Coba lagi", key="btn_auth_retry")
        st.stop()

    if user is None:
        _render_login(st)
        st.stop()

    allowed = roles or VALID_ROLES
    if user["role"] not in allowed:
        # Drop any OAuth ?code= still in the URL: it must not outlive a denied visit.
        st.query_params.clear()
        st.error("❌ Akses ditolak: halaman ini hanya untuk " + " / ".join(allowed) + ".")
        if st.button("Logout", key="btn_auth_denied_logout"):
            logout(clear_all=True)
            st.rerun()
        st.stop()
    return user


def fragment_guard(touch=False):
    """First line of every @st.fragment body. A fragment rerun skips the
    page-level gate, so re-check here; if the session is gone (expired,
    disabled, logged out) trigger a full-app rerun so gate() takes over."""
    import streamlit as st
    try:
        user = current_user(touch=touch)
    except AuthError:
        user = None
    if user is None:
        st.rerun()  # scope="app": gate() renders login / error
    return user


def render_user_box():
    """Sidebar: who is logged in + Logout."""
    import streamlit as st
    try:
        user = current_user(touch=False)
    except AuthError:
        user = None
    if not user:
        return
    st.caption(f"👤 {user['label']} · {user['role']}")
    if st.button("Logout", key="btn_logout"):
        logout(clear_all=True)
        st.rerun()
