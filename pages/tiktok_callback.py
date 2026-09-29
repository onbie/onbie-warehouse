"""
pages/tiktok_callback.py
=========================
TikTok Shop seller OAuth callback route:
    https://onbie-packing.streamlit.app/tiktok_callback

Streamlit auto-routes any file under pages/ to /<filename-without-.py>.

What this page does:
    1. Bridges TIKTOK_APP_KEY / TIKTOK_APP_SECRET from st.secrets into
       os.environ (tiktok_auth.py reads os.environ, like shopee_auth.py).
    2. If TikTok redirected here with a `code`, hands it to
       tiktok_auth.handle_oauth_callback(): token exchange -> authorized
       shop lookup -> local token save.
    3. Shows only non-secret results (seller/shop names, region, scopes).

What this page never does:
    - Display, log, or store in session state the authorization code,
      app secret, access token, refresh token, or shop_cipher. The only
      thing kept in st.session_state is the display-safe summary
      returned by tiktok_auth.handle_oauth_callback().
    - Touch app.py, Shopee OAuth/API code, packed.csv, packed_snapshots.csv,
      or Supabase.
    - Sync orders (not implemented yet).

The seller authorization link is NOT constructed here. It is the exact URL
from Partner Center's "Copy authorization link", supplied via the optional
Streamlit secret TIKTOK_AUTH_URL.
"""

import os
import sys

import streamlit as st

# Make the repo root importable regardless of how Streamlit sets sys.path
# for pages/ scripts (tiktok_auth.py lives next to app.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import tiktok_auth  # noqa: E402

RESULT_KEY = "_tiktok_oauth_result"

st.set_page_config(page_title="TikTok OAuth Callback", page_icon="🎵")
st.title("🎵 TikTok Shop OAuth Callback")


def _bridge_secrets():
    """st.secrets -> os.environ, the same pattern app.py uses for Shopee and
    Supabase, scoped to this page. A missing secret (or no secrets file at
    all) is skipped silently here — tiktok_auth.get_credentials() reports
    exactly which names are missing, without ever echoing a value."""
    for name in ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET"):
        try:
            value = str(st.secrets[name]).strip()
        except Exception:
            continue
        if value:
            os.environ[name] = value


_bridge_secrets()

# ---- Handle the OAuth redirect ----------------------------------------
# The auth code is single-use and secret. Take it, remove it from the URL
# immediately (so a refresh or a shared/bookmarked URL can't replay it),
# and never display it or keep it anywhere but this local variable.
_code = st.query_params.get("code", "")
if _code:
    st.query_params.clear()
    with st.spinner("Menukar authorization code dengan token TikTok Shop..."):
        try:
            _summary = tiktok_auth.handle_oauth_callback(_code)
            st.session_state[RESULT_KEY] = {"ok": True, "summary": _summary}
        except tiktok_auth.TikTokAuthError as e:
            # TikTokAuthError messages are display-safe by construction.
            st.session_state[RESULT_KEY] = {"ok": False, "message": str(e)}
        except Exception as e:
            # Deliberately NOT str(e): an unexpected exception's text could
            # contain a request URL (and therefore a credential).
            st.session_state[RESULT_KEY] = {
                "ok": False,
                "message": f"Unexpected error during TikTok authorization ({type(e).__name__}).",
            }
    _code = None

# ---- Render status ------------------------------------------------------
_result = st.session_state.get(RESULT_KEY)

if _result is None:
    st.info("🎵 TikTok OAuth callback ready.")
    st.caption("Waiting for a redirect from TikTok Shop with an authorization code.")

elif _result["ok"]:
    _summary = _result["summary"]
    st.success("✅ TikTok Shop berhasil diotorisasi. Token tersimpan.")

    if _summary.get("seller_name"):
        st.write(f"**Seller:** {_summary['seller_name']}")
    if _summary.get("seller_base_region"):
        st.write(f"**Region:** {_summary['seller_base_region']}")

    st.write(f"**Authorized shop(s): {len(_summary['shops'])}**")
    for _shop in _summary["shops"]:
        st.write(
            f"- {_shop.get('name') or '-'} "
            f"({_shop.get('region') or '-'}, {_shop.get('seller_type') or '-'})"
        )

    if _summary["granted_scopes"]:
        st.write("**Granted scopes:** " + ", ".join(_summary["granted_scopes"]))
    if _summary["missing_scopes"]:
        st.warning(
            "⚠️ Scope berikut tidak muncul di daftar granted scopes: "
            + ", ".join(_summary["missing_scopes"])
            + ". Cek konfigurasi Manage API di Partner Center."
        )

    st.caption(
        "Token disimpan sementara di tiktok_tokens.json (hilang saat Streamlit Cloud "
        "restart; Supabase belum diaktifkan untuk TikTok). Jangan commit file itu ke git."
    )

else:
    st.error(f"❌ {_result['message']}")

# ---- Credential / authorization-link status (booleans only) ------------
_missing_credentials = [
    name for name in ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET") if not os.environ.get(name, "").strip()
]
if _missing_credentials:
    st.warning("Secret belum diset: " + ", ".join(_missing_credentials))

# Optional. Paste the EXACT link from Partner Center > App & Service >
# your service > "Copy authorization link" into the TIKTOK_AUTH_URL secret.
# It is never built or altered here.
try:
    _auth_url = str(st.secrets["TIKTOK_AUTH_URL"]).strip()
except Exception:
    _auth_url = ""

st.divider()
if _auth_url.startswith("https://"):
    if tiktok_auth.TIKTOK_SERVICE_ID not in _auth_url:
        st.warning("TIKTOK_AUTH_URL tidak mengandung Service ID Onbie — pastikan link-nya benar.")
    st.link_button("🎵 Otorisasi TikTok Shop", _auth_url)
else:
    st.caption(
        "Untuk memulai otorisasi, tambahkan link dari Partner Center → "
        "\"Copy authorization link\" sebagai secret TIKTOK_AUTH_URL."
    )
