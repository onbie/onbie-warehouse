-- =====================================================================
-- Onbie Packing System - login setup (OWNER: Supabase Auth, PACKER: ID + PIN)
-- ---------------------------------------------------------------------
-- NOT executed by the app. Review, then run by hand in the Supabase SQL
-- Editor. This file does NOT touch packed_orders / packed_snapshot_items.
--
-- Design in one paragraph
--   OWNER  = Supabase Auth (email + password); authorization row in `profiles`.
--   PACKER = 2-digit ID (assigned automatically, never reused) + 6-digit PIN
--            (generated randomly by the app). PACKERs are NOT Supabase Auth
--            users, so no synthetic emails exist. The app never sends the
--            raw PIN anywhere: it sends HMAC-SHA256(PIN_PEPPER, "id:pin")
--            (64 hex chars); only a bcrypt hash of that value is stored.
--            Verification AND lockout bookkeeping happen inside ONE Postgres
--            function (packer_login) under a row lock, so concurrent guesses
--            cannot race the counters. Only the service_role key can call it,
--            and the service_role cannot even read pin_hash.
-- =====================================================================

create extension if not exists pgcrypto with schema extensions;

-- ---------------------------------------------------------------------
-- STEP 1 - profiles: OWNER accounts (Supabase Auth users)
-- ---------------------------------------------------------------------
create table if not exists public.profiles (
    user_id    uuid primary key references auth.users (id) on delete cascade,
    email      text        not null,
    role       text        not null check (role = 'OWNER'),
    is_active  boolean     not null default true,
    created_at timestamptz not null default now()
);
alter table public.profiles enable row level security;   -- no policies: server (service_role) only
revoke all on public.profiles from anon, authenticated;

-- ---------------------------------------------------------------------
-- STEP 2 - packer_accounts: PACKER accounts (ID + PIN)
-- ---------------------------------------------------------------------
-- IDs come from this sequence (01..99, no cycle): monotonic, so an ID is
-- never handed out twice. Accounts are never deleted (trigger below), only
-- disabled, so an old ID can never be reassigned to a new person either.
create sequence if not exists public.packer_code_seq
    minvalue 1 maxvalue 99 start 1 no cycle;

create table if not exists public.packer_accounts (
    user_id          uuid primary key default gen_random_uuid(),
    user_code        text        not null unique
                     check (user_code ~ '^[0-9]{2}$' and user_code <> '00'),
    pin_hash         text        not null,            -- bcrypt(HMAC(pepper,"id:pin")); never readable by the app role
    is_active        boolean     not null default true,
    session_epoch    bigint      not null default 0,  -- bumped on disable / PIN reset: kills live sessions
    failed_streak    integer     not null default 0,  -- consecutive failures (reset by a success)
    window_started_at timestamptz,
    window_failures  integer     not null default 0,  -- rolling 30-day failures (NOT reset by a success)
    locked_until     timestamptz,
    last_failed_at   timestamptz,
    pin_changed_at   timestamptz not null default now(),
    created_at       timestamptz not null default now()
);

-- Earlier draft had a permanent "hard lock"; replaced by a 12-hour lock (see packer_login).
alter table public.packer_accounts drop column if exists hard_locked;

alter table public.packer_accounts enable row level security;   -- no policies
revoke all on public.packer_accounts from public, anon, authenticated, service_role;
-- The app may read status columns - never pin_hash. All writes go through the functions below.
grant select (user_id, user_code, is_active, session_epoch, failed_streak, window_failures,
              window_started_at, locked_until, last_failed_at, pin_changed_at, created_at)
    on public.packer_accounts to service_role;
revoke all on sequence public.packer_code_seq from public, anon, authenticated, service_role;

create or replace function public.packer_guard_changes() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' or tg_op = 'TRUNCATE' then
        raise exception 'packer accounts are never deleted (IDs are never reused); disable the account instead';
    end if;
    if new.user_code is distinct from old.user_code then
        raise exception 'packer user_code is immutable';
    end if;
    return new;
end $$;

drop trigger if exists packer_no_delete on public.packer_accounts;
create trigger packer_no_delete before delete on public.packer_accounts
    for each row execute function public.packer_guard_changes();
drop trigger if exists packer_no_truncate on public.packer_accounts;
create trigger packer_no_truncate before truncate on public.packer_accounts
    for each statement execute function public.packer_guard_changes();
drop trigger if exists packer_code_immutable on public.packer_accounts;
create trigger packer_code_immutable before update on public.packer_accounts
    for each row execute function public.packer_guard_changes();

-- ---------------------------------------------------------------------
-- STEP 3 - functions (SECURITY DEFINER; executable by service_role only)
-- ---------------------------------------------------------------------

-- Reserve the next ID. Consumed even if the later create fails: never reused.
create or replace function public.packer_reserve_code() returns text
language plpgsql security definer set search_path = pg_catalog, public, extensions as $$
declare n bigint;
begin
    n := nextval('public.packer_code_seq');      -- errors when 99 is exhausted (no cycle)
    return lpad(n::text, 2, '0');
end $$;

-- p_secret = 64-hex HMAC derived by the app, NOT the PIN.
create or replace function public.packer_create(p_code text, p_secret text) returns jsonb
language plpgsql security definer set search_path = pg_catalog, public, extensions as $$
declare
    v_id uuid;
    v_last bigint; v_called boolean;
begin
    if p_code is null or p_code !~ '^[0-9]{2}$' or p_code = '00' then
        raise exception 'invalid packer code';
    end if;
    if p_secret is null or p_secret !~ '^[0-9a-f]{64}$' then
        raise exception 'invalid secret format';
    end if;
    select last_value, is_called into v_last, v_called from public.packer_code_seq;
    if not v_called or p_code::int > v_last then
        raise exception 'packer code was not reserved';
    end if;
    insert into public.packer_accounts (user_code, pin_hash)
    values (p_code, crypt(p_secret, gen_salt('bf', 10)))
    returning user_id into v_id;
    return jsonb_build_object('user_id', v_id, 'user_code', p_code);
end $$;

-- The login attempt. Verification + lockout in ONE transaction under a row
-- lock (concurrent attempts on the same account are serialized, so counters
-- cannot be raced). Status values:
--   ok | invalid | locked (+retry_after_seconds) | inactive
-- Policy (documented trade-offs in the README/report):
--   * 5 consecutive failures  -> locked 5 min   (each further failure re-locks)
--   * 10 consecutive failures -> locked 30 min
--   * 15 failures within a rolling 30 days (a success does NOT reset this)
--                              -> locked 12 HOURS (each further failure re-locks 12 h);
--                                 an OWNER can unlock earlier (packer_unlock / packer_unlock_all)
--     DoS trade-off: anyone who can reach the login page can lock IDs (about 15 requests
--     per ID), but a lock now expires by itself after 12 h instead of waiting for an OWNER.
--   * attempts while locked are NOT verified (no oracle) and NOT counted
create or replace function public.packer_login(p_code text, p_secret text) returns jsonb
language plpgsql security definer set search_path = pg_catalog, public, extensions as $$
declare
    a public.packer_accounts%rowtype;
    v_now timestamptz := clock_timestamp();
begin
    if p_code is null or p_code !~ '^[0-9]{2}$' or p_secret is null or p_secret !~ '^[0-9a-f]{64}$' then
        return jsonb_build_object('status', 'invalid');
    end if;

    select * into a from public.packer_accounts where user_code = p_code for update;
    if not found then
        return jsonb_build_object('status', 'invalid');   -- an ID is not a secret; no state to track
    end if;

    if a.locked_until is not null and a.locked_until > v_now then
        return jsonb_build_object('status', 'locked',
            'retry_after_seconds', ceil(extract(epoch from (a.locked_until - v_now)))::int);
    end if;

    if a.window_started_at is null or v_now - a.window_started_at > interval '30 days' then
        a.window_started_at := v_now;
        a.window_failures := 0;
    end if;

    if crypt(p_secret, a.pin_hash) = a.pin_hash then
        update public.packer_accounts
           set failed_streak = 0, locked_until = null,
               window_started_at = a.window_started_at, window_failures = a.window_failures
         where user_id = a.user_id;
        if not a.is_active then
            return jsonb_build_object('status', 'inactive');
        end if;
        return jsonb_build_object('status', 'ok', 'user_id', a.user_id,
                                  'user_code', a.user_code, 'session_epoch', a.session_epoch);
    end if;

    a.failed_streak := a.failed_streak + 1;
    a.window_failures := a.window_failures + 1;
    a.last_failed_at := v_now;
    if a.window_failures >= 15 then
        a.locked_until := v_now + interval '12 hours';
    elsif a.failed_streak >= 10 then
        a.locked_until := v_now + interval '30 minutes';
    elsif a.failed_streak >= 5 then
        a.locked_until := v_now + interval '5 minutes';
    end if;

    update public.packer_accounts
       set failed_streak = a.failed_streak, window_started_at = a.window_started_at,
           window_failures = a.window_failures, last_failed_at = a.last_failed_at,
           locked_until = a.locked_until
     where user_id = a.user_id;

    if a.locked_until is not null and a.locked_until > v_now then
        return jsonb_build_object('status', 'locked',
            'retry_after_seconds', ceil(extract(epoch from (a.locked_until - v_now)))::int);
    end if;
    return jsonb_build_object('status', 'invalid');
end $$;

-- New PIN (OWNER reset): clears every lock/counter, kills live sessions.
create or replace function public.packer_set_pin(p_user_id uuid, p_secret text) returns void
language plpgsql security definer set search_path = pg_catalog, public, extensions as $$
begin
    if p_secret is null or p_secret !~ '^[0-9a-f]{64}$' then
        raise exception 'invalid secret format';
    end if;
    update public.packer_accounts
       set pin_hash = crypt(p_secret, gen_salt('bf', 10)), pin_changed_at = now(),
           session_epoch = session_epoch + 1,
           failed_streak = 0, window_started_at = null, window_failures = 0,
           locked_until = null
     where user_id = p_user_id;
    if not found then raise exception 'packer not found'; end if;
end $$;

-- Disable / enable. Disabling also kills live sessions.
create or replace function public.packer_set_active(p_user_id uuid, p_active boolean) returns void
language plpgsql security definer set search_path = pg_catalog, public, extensions as $$
begin
    update public.packer_accounts
       set is_active = p_active,
           session_epoch = case when p_active then session_epoch else session_epoch + 1 end
     where user_id = p_user_id;
    if not found then raise exception 'packer not found'; end if;
end $$;

-- OWNER unlock: clears the lock (5 min / 30 min / 12 h) and the failure counters.
create or replace function public.packer_unlock(p_user_id uuid) returns void
language plpgsql security definer set search_path = pg_catalog, public, extensions as $$
begin
    update public.packer_accounts
       set failed_streak = 0, window_started_at = null, window_failures = 0,
           locked_until = null
     where user_id = p_user_id;
    if not found then raise exception 'packer not found'; end if;
end $$;

-- OWNER unlock-all: clears every lock and failure counter. Returns how many accounts
-- were locked at that moment. (The explicit confirmation lives in the app.)
create or replace function public.packer_unlock_all() returns integer
language plpgsql security definer set search_path = pg_catalog, public, extensions as $$
declare n integer;
begin
    select count(*) into n from public.packer_accounts where locked_until > clock_timestamp();
    update public.packer_accounts
       set failed_streak = 0, window_started_at = null, window_failures = 0, locked_until = null
     where locked_until is not null or failed_streak > 0 or window_failures > 0 or window_started_at is not null;
    return n;
end $$;

-- Only the server's service_role key may call these (not anon / logged-in users).
revoke all on function public.packer_reserve_code()            from public, anon, authenticated;
revoke all on function public.packer_create(text, text)        from public, anon, authenticated;
revoke all on function public.packer_login(text, text)         from public, anon, authenticated;
revoke all on function public.packer_set_pin(uuid, text)       from public, anon, authenticated;
revoke all on function public.packer_set_active(uuid, boolean) from public, anon, authenticated;
revoke all on function public.packer_unlock(uuid)              from public, anon, authenticated;
revoke all on function public.packer_unlock_all()              from public, anon, authenticated;
grant execute on function public.packer_reserve_code()            to service_role;
grant execute on function public.packer_create(text, text)        to service_role;
grant execute on function public.packer_login(text, text)         to service_role;
grant execute on function public.packer_set_pin(uuid, text)       to service_role;
grant execute on function public.packer_set_active(uuid, boolean) to service_role;
grant execute on function public.packer_unlock(uuid)              to service_role;
grant execute on function public.packer_unlock_all()              to service_role;

-- ---------------------------------------------------------------------
-- STEP 4 - first OWNER (manual; the app only ever creates PACKERs)
-- 1. Supabase Dashboard > Authentication > Users > "Add user" (tick
--    "Auto Confirm User"), strong password (12+ chars recommended).
-- 2. Copy that user's UUID and run (replace the placeholders):
-- ---------------------------------------------------------------------
-- insert into public.profiles (user_id, email, role, is_active)
-- values ('00000000-0000-0000-0000-000000000000', 'owner@example.com', 'OWNER', true);
--
-- Create at least TWO owners so there is always a way back in (and a way
-- to unlock a hard-locked PACKER).

-- ---------------------------------------------------------------------
-- STEP 5 - OPTIONAL hardening of existing tables (REVIEW FIRST)
-- Only if SUPABASE_KEY is the service_role key (otherwise the app would
-- lose access to its own data).
-- ---------------------------------------------------------------------
-- alter table public.shopee_tokens          enable row level security;
-- alter table public.packed_orders          enable row level security;
-- alter table public.packed_snapshot_items  enable row level security;

-- ---------------------------------------------------------------------
-- STEP 6 - Dashboard settings and secrets (cannot be done in SQL)
-- Authentication > Sign In / Providers > Email:
--   * "Allow new users to sign up" -> OFF (no public self-registration)
--   * Minimum password length       -> 10+ (applies to OWNER accounts)
-- Streamlit Cloud > App settings > Secrets, add:
--   SUPABASE_ANON_KEY = "<anon / publishable key - OWNER login only>"
--   PIN_PEPPER        = "<64+ random chars, e.g. python -c 'import secrets;print(secrets.token_hex(32))'>"
--   (SUPABASE_URL / SUPABASE_KEY exist already; SUPABASE_KEY must stay the
--    service-role key and never leave the server.)
-- Rotating PIN_PEPPER invalidates every PIN: all PACKERs need a new PIN
-- (OWNER panel > Reset PIN). Store the pepper somewhere safe.
-- =====================================================================
