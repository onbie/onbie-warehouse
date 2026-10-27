"""
account_admin.py
================
OWNER-only management of PACKER accounts (ID + PIN): create, disable /
enable, reset PIN, unlock. v1 has no public signup and the app can only ever
create PACKER accounts - the first OWNER is created manually in Supabase.

Every function re-checks `auth_guard.require_role(OWNER)` itself, so
authorization is enforced server-side regardless of which UI rendered the
call.

Security properties
-------------------
* IDs are assigned by Postgres (packer_reserve_code, a no-cycle sequence):
  automatic, monotonic, never reused. Accounts are never deleted.
* PINs are generated here with `secrets` (random, weak patterns rejected) and
  shown to the OWNER exactly once. Only HMAC-SHA256(PIN_PEPPER, "id:pin")
  is sent to Supabase; only its bcrypt hash is stored. The raw PIN is never
  stored, logged or sent to Supabase.
* Every state change goes through SECURITY DEFINER functions that only the
  service_role may execute. Disable and PIN reset bump `session_epoch`, which
  ends the account's live sessions at their next revalidation (<= 60 s).
* The status columns the app reads never include pin_hash (the service_role
  has no privilege on that column).
"""

import logging
import secrets
import uuid

import auth_guard
from auth_guard import AuthError, ROLE_OWNER, PACKER_TABLE

logger = logging.getLogger(__name__)

_STATUS_COLUMNS = (
    "user_id,user_code,is_active,session_epoch,failed_streak,window_failures,"
    "locked_until,last_failed_at,created_at"
)


# --------------------------------------------------------------------------
# PIN generation
# --------------------------------------------------------------------------
def _is_weak_pin(pin):
    digits = [int(c) for c in pin]
    if len(set(pin)) == 1:                                   # 000000, 111111 ...
        return True
    steps = {digits[i + 1] - digits[i] for i in range(len(digits) - 1)}
    if steps in ({1}, {-1}):                                 # 123456, 654321 ...
        return True
    if pin[:3] == pin[3:] or pin[:2] * 3 == pin:             # 123123, 121212 ...
        return True
    return False


def generate_pin():
    """Random 6-digit PIN (CSPRNG), trivial patterns rejected."""
    while True:
        pin = f"{secrets.randbelow(10 ** 6):06d}"
        if not _is_weak_pin(pin):
            return pin


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _owner():
    return auth_guard.require_role(ROLE_OWNER)


def _admin_error(action, exc):
    logger.warning("account admin %s failed: %s", action, type(exc).__name__)
    detail = auth_guard.scrub(str(exc).strip().replace("\n", " "))[:200]
    return AuthError(f"Gagal {action}: {detail or type(exc).__name__}")


def _rpc(action, name, params):
    client = auth_guard._data_client()
    try:
        return client.rpc(name, params).execute().data
    except Exception as e:
        raise _admin_error(action, e) from e


def _valid_uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise AuthError("ID akun tidak valid.") from None


def _get_packer(client, user_id):
    user_id = _valid_uuid(user_id)
    try:
        rows = (
            client.table(PACKER_TABLE).select(_STATUS_COLUMNS)
            .eq("user_id", user_id).execute().data
        ) or []
    except Exception as e:
        raise _admin_error("membaca akun", e) from e
    if not rows:
        raise AuthError("Akun tidak ditemukan.")
    return rows[0]


# --------------------------------------------------------------------------
# Operations (all OWNER-only)
# --------------------------------------------------------------------------
def list_packers():
    _owner()
    client = auth_guard._data_client()
    try:
        return (
            client.table(PACKER_TABLE).select(_STATUS_COLUMNS).order("user_code").execute().data
        ) or []
    except Exception as e:
        raise _admin_error("membaca daftar akun", e) from e


def create_packer():
    """Create the next PACKER account. Returns {"user_id","user_code","pin"};
    the PIN exists only in this return value - show it once, never keep it."""
    _owner()
    code = _rpc("membuat ID akun", "packer_reserve_code", {})
    if not auth_guard.valid_code(code):
        raise AuthError("Gagal membuat ID akun (ID mungkin sudah habis: maksimal 99).")
    pin = generate_pin()
    secret = auth_guard.derive_pin_secret(code, pin)
    try:
        out = _rpc("membuat akun", "packer_create", {"p_code": code, "p_secret": secret})
    except AuthError as e:
        raise AuthError(f"{e} (ID {code} sudah terpakai dan tidak akan dipakai ulang.)") from None
    if not isinstance(out, dict) or not out.get("user_id") or out.get("user_code") != code:
        raise AuthError("Respons pembuatan akun tidak valid.")
    return {"user_id": str(out["user_id"]), "user_code": code, "pin": pin}


def reset_packer_pin(user_id):
    """New random PIN: clears every lock and ends live sessions."""
    _owner()
    client = auth_guard._data_client()
    packer = _get_packer(client, user_id)
    pin = generate_pin()
    secret = auth_guard.derive_pin_secret(packer["user_code"], pin)
    _rpc("mereset PIN", "packer_set_pin", {"p_user_id": packer["user_id"], "p_secret": secret})
    return {"user_code": packer["user_code"], "pin": pin}


def set_packer_active(user_id, active):
    """Disable (ends live sessions) or enable."""
    _owner()
    client = auth_guard._data_client()
    packer = _get_packer(client, user_id)
    _rpc("mengubah status akun", "packer_set_active",
         {"p_user_id": packer["user_id"], "p_active": bool(active)})


def unlock_packer(user_id):
    """Clear the lock (5 min / 30 min / 12 h) and the failure counters."""
    _owner()
    client = auth_guard._data_client()
    packer = _get_packer(client, user_id)
    _rpc("membuka kunci akun", "packer_unlock", {"p_user_id": packer["user_id"]})


def unlock_all_packers(confirm=False):
    """Clear every PACKER lock and failure counter at once. Refuses unless the
    caller passes confirm=True (the panel only does so after an explicit second
    click). Returns how many accounts were locked at that moment."""
    _owner()
    if confirm is not True:
        raise AuthError("Unlock semua akun membutuhkan konfirmasi.")
    out = _rpc("membuka kunci semua akun", "packer_unlock_all", {})
    if isinstance(out, bool) or not isinstance(out, int) or out < 0:
        raise AuthError("Respons buka kunci semua akun tidak valid.")
    return out


# --------------------------------------------------------------------------
# Streamlit panel (sidebar expander, rendered for OWNER only)
# --------------------------------------------------------------------------
_NOTICE_KEY = "_packer_pin_notice"


def _lock_text(p):
    if p.get("locked_until"):
        return f"⏳ terkunci sementara s/d {p['locked_until']}"
    return ""


_UNLOCK_ALL_KEY = "_packer_unlock_all_pending"


def _render_unlock_all(st):
    """'Unlock All' with an explicit second step: nothing runs on the first click."""
    if not st.session_state.get(_UNLOCK_ALL_KEY):
        if st.button("🔓 Unlock Semua Akun PACKER", key="btn_unlock_all"):
            st.session_state[_UNLOCK_ALL_KEY] = True
            st.rerun()
        return
    st.warning(
        "Ini membuka kunci SEMUA akun PACKER dan mereset hitungan gagal login mereka. "
        "Lanjutkan hanya jika penguncian itu bukan dari percobaan masuk yang mencurigakan, "
        "atau PIN sudah diganti."
    )
    c1, c2 = st.columns(2)
    try:
        if c1.button("✅ Ya, buka semua", key="btn_unlock_all_confirm"):
            st.session_state.pop(_UNLOCK_ALL_KEY, None)
            n = unlock_all_packers(confirm=True)
            st.session_state["_packer_unlock_all_done"] = n
            st.rerun()
        if c2.button("Batal", key="btn_unlock_all_cancel"):
            st.session_state.pop(_UNLOCK_ALL_KEY, None)
            st.rerun()
    except AuthError as e:
        st.error(f"❌ {e}")


def render_owner_panel():
    import streamlit as st

    try:
        _owner()
    except AuthError:
        return  # not an OWNER (or session gone): render nothing

    # A freshly issued PIN is shown ONCE: popped here, so the next rerun no longer has it.
    notice = st.session_state.pop(_NOTICE_KEY, None)
    if notice:
        st.success(f"PIN baru untuk **ID {notice['user_code']}**:")
        st.code(notice["pin"])
        st.caption("PIN hanya tampil sekali. Catat dan serahkan sekarang; tidak bisa dilihat lagi.")

    done = st.session_state.pop("_packer_unlock_all_done", None)
    if done is not None:
        st.success(f"Kunci semua akun PACKER dibuka ({done} akun sedang terkunci).")

    st.caption("Hanya OWNER. ID dibuat otomatis (tidak dipakai ulang). PIN 6 digit dibuat acak.")

    try:
        packers = list_packers()
    except AuthError as e:
        st.error(f"❌ {e}")
        packers = []

    for p in packers:
        uid = p["user_id"]
        status = "✅ aktif" if p.get("is_active") is True else "⛔ nonaktif"
        st.markdown(f"**ID {p.get('user_code', '--')}** — {status}")
        lock = _lock_text(p)
        if lock:
            st.caption(lock)
        elif p.get("window_failures"):
            st.caption(f"Gagal login (30 hari): {p['window_failures']}")
        c1, c2, c3 = st.columns(3)
        try:
            if p.get("is_active") is True:
                if c1.button("Nonaktifkan", key=f"btn_disable_{uid}"):
                    set_packer_active(uid, False)
                    st.rerun()
            else:
                if c1.button("Aktifkan", key=f"btn_enable_{uid}"):
                    set_packer_active(uid, True)
                    st.rerun()
            if c2.button("Reset PIN", key=f"btn_resetpin_{uid}"):
                st.session_state[_NOTICE_KEY] = reset_packer_pin(uid)
                st.rerun()
            if (p.get("locked_until") or p.get("window_failures")) \
                    and c3.button("Buka kunci", key=f"btn_unlock_{uid}"):
                unlock_packer(uid)
                st.rerun()
        except AuthError as e:
            st.error(f"❌ {e}")

    if packers:
        _render_unlock_all(st)

    st.divider()
    if st.button("➕ Buat akun PACKER baru", key="btn_create_packer"):
        try:
            st.session_state[_NOTICE_KEY] = create_packer()
        except AuthError as e:
            st.error(f"❌ {e}")
        else:
            st.rerun()
