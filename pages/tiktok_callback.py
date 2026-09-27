"""
pages/tiktok_callback.py
=========================
Minimal TikTok Shop OAuth callback route.

Streamlit auto-discovers any file under pages/ next to app.py and routes
it to /<filename-without-.py> — so this file alone makes
https://onbie-packing.streamlit.app/tiktok_callback exist and be
reachable, with ZERO changes to app.py or any existing module.

Scope, deliberately minimal:
    - Detects whether TikTok's `code` query parameter is present.
    - NEVER displays the code value — not even partially/masked.
    - Does NOT exchange the code for a token (no network calls at all).
    - Does NOT touch app_key / app_secret / access tokens / any secret.
    - Does NOT import or call shopee_auth, shopee_api, or Supabase.
    - Does NOT read/write packed.csv, packed_snapshots.csv, or any other
      production file.

This is intentionally just a reachability + wiring check. Token exchange,
credential handling, and shop discovery are separate, later steps.
"""

import streamlit as st

st.set_page_config(page_title="TikTok OAuth Callback", page_icon="🎵")

st.title("🎵 TikTok Shop OAuth Callback")

# Detect presence of TikTok's authorization code WITHOUT ever reading it
# into a variable that gets displayed, logged, or stored. `code` below is
# only ever used in a boolean/truthiness check.
code_present = bool(st.query_params.get("code", ""))

if code_present:
    st.success("✅ Authorization code received from TikTok Shop.")
    st.caption(
        "The code itself is never shown here. Token exchange is not "
        "implemented yet — this page only confirms the callback route works."
    )
else:
    st.info("🎵 TikTok OAuth callback ready.")
    st.caption("Waiting for a redirect from TikTok Shop with an authorization code.")
