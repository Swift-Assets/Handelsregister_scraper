-- =============================================================================
-- 0001 — Central request budget, ledger and raw-document store for the
--        Registerportal worker.
--
-- WHY THIS EXISTS
--   The worker package shipped with an in-memory sliding-window budget. A
--   restart, a second process or a crash reset it to zero, so the portal's
--   60-retrievals-per-hour rule was enforced by nothing durable. This migration
--   moves the budget into the database, where it is the ONLY counter: the
--   worker owns no counter of its own and must ask before every single
--   request it makes.
--
--   It also makes the safe mode the DEFAULT, so the system is correct while the
--   Whitelist-IP application at the Servicestelle Registerportal (AG Hagen) is
--   unanswered, and stays correct if the answer is negative. Nothing has to be
--   changed for that case; only an approval would change anything.
--
-- PRODUCTION & CUSTOMER IMPACT
--   None. Three new tables, four new functions, one new nullable column on
--   swift_v2.source_handelsregister_records. No existing row is read, written,
--   moved or deleted; no existing function, view, RPC or cron job changes
--   behaviour. No customer-facing surface touches any of this. The worker that
--   uses it is not scheduled anywhere yet and refuses to write while
--   HR_DRY_RUN=1 (its default).
--
-- ROLLBACK
--   drop function if exists swift_v2.registry_claim_request(text,text,uuid,text,text);
--   drop function if exists swift_v2.registry_finish_request(uuid,text,integer,text);
--   drop function if exists swift_v2.registry_open_circuit(text,text,integer);
--   drop function if exists swift_v2.registry_budget_status(text);
--   drop view if exists swift_v2.v_registry_budget_status;
--   drop table if exists swift_v2.registry_request_ledger;
--   drop table if exists swift_v2.registry_raw_documents;
--   drop table if exists swift_v2.registry_source_config;
--   alter table swift_v2.source_handelsregister_records
--     drop column if exists raw_document_sha256;
-- =============================================================================

-- 1) Configuration -----------------------------------------------------------
-- One row per external source. This is the single place the operator changes
-- to raise or lower the cap, pause the source, or record a granted Whitelist.

create table if not exists swift_v2.registry_source_config (
    source                 text primary key,
    enabled                boolean     not null default false,
    -- Caps the worker must obey. Both are enforced, on sliding windows.
    max_per_hour           integer     not null default 40,
    max_per_day            integer     not null default 700,
    -- Politeness floor between two requests, independent of the caps.
    min_seconds_between    integer     not null default 45,
    -- Hours (UTC) in which the source may be contacted at all. A single IP
    -- hammering around the clock is the most bot-like signature there is, and
    -- our Whitelist application promised requests spread over business hours.
    allowed_hour_start_utc smallint    not null default 5,
    allowed_hour_end_utc   smallint    not null default 22,
    -- Set to true ONLY after the Servicestelle grants a registered IP, and
    -- only with the granted figures. Until then the ceilings below apply.
    whitelist_granted      boolean     not null default false,
    whitelist_note         text,
    -- Written by the circuit breaker. While in the future, nothing is granted.
    cooldown_until         timestamptz,
    cooldown_reason        text,
    updated_at             timestamptz not null default now(),
    updated_by             text,
    constraint registry_source_config_hours_ck
        check (allowed_hour_start_utc between 0 and 23
           and allowed_hour_end_utc   between 1 and 24
           and allowed_hour_start_utc < allowed_hour_end_utc),
    constraint registry_source_config_positive_ck
        check (max_per_hour >= 1 and max_per_day >= 1 and min_seconds_between >= 0),
    -- The hard safety rule, in the schema rather than in code:
    --   without a Whitelist   -> at most 55/h and 900/day (portal rule is 60/h;
    --                            55 keeps a margin for anything we miscount)
    --   with a Whitelist      -> at most the figures we applied for, 150/h and
    --                            1000/day, never more
    constraint registry_source_config_ceiling_ck
        check (
            (not whitelist_granted and max_per_hour <= 55  and max_per_day <= 900)
         or (    whitelist_granted and max_per_hour <= 150 and max_per_day <= 1000)
        )
);

alter table swift_v2.registry_source_config enable row level security;
revoke all on swift_v2.registry_source_config from public, anon, authenticated;

comment on table swift_v2.registry_source_config is
  '0001: the one place the Registerportal request caps are set. The ceiling '
  'check refuses any value above the portal rule (55/h, 900/day) unless a '
  'Whitelist-IP has been granted, and above 150/h, 1000/day in any case.';

insert into swift_v2.registry_source_config (source, enabled, updated_by)
values ('handelsregister', false, 'migration:0001')
on conflict (source) do nothing;


-- 2) Request ledger ----------------------------------------------------------
-- Append-only. One row per request the worker is allowed to make, written
-- BEFORE the request leaves the machine, so a crash mid-request still costs
-- budget. This is what "count conservatively" means in practice.

create table if not exists swift_v2.registry_request_ledger (
    id                    uuid primary key default gen_random_uuid(),
    source                text        not null,
    request_kind          text        not null,
    requested_at          timestamptz not null default now(),
    finished_at           timestamptz,
    run_id                text,
    entity_id             uuid,
    registry_identity_key text,
    outcome               text        not null default 'claimed',
    http_status           integer,
    note                  text,
    constraint registry_request_ledger_kind_ck
        check (request_kind in ('session_open','search','document','other'))
);

create index if not exists registry_request_ledger_source_time_idx
    on swift_v2.registry_request_ledger (source, requested_at desc);
create index if not exists registry_request_ledger_run_idx
    on swift_v2.registry_request_ledger (run_id) where run_id is not null;

alter table swift_v2.registry_request_ledger enable row level security;
revoke all on swift_v2.registry_request_ledger from public, anon, authenticated;

comment on table swift_v2.registry_request_ledger is
  '0001: append-only record of every request made to an external register '
  'portal. It is the budget counter itself, not a report about it.';


-- 3) Raw documents -----------------------------------------------------------
-- Evaluation store, deliberately temporary. Content-addressed and gzipped, so
-- the same document fetched twice costs nothing the second time. keep_until
-- exists so this cannot quietly become permanent bloat: the mirror already
-- taught us what duplicated text does to this database.
--
-- Natural-person birth dates and private addresses are stripped BEFORE the
-- bytes reach this table (handelsregister/xjustiz.py::redact_si). What is
-- stored is the redacted document, never the original.

create table if not exists swift_v2.registry_raw_documents (
    content_sha256  text        primary key,
    source          text        not null,
    document_kind   text        not null,
    content_gzip    bytea       not null,
    bytes_original  integer     not null,
    bytes_stored    integer     not null,
    redaction_rule  text        not null,
    first_seen_at   timestamptz not null default now(),
    keep_until      timestamptz not null default (now() + interval '90 days')
);

alter table swift_v2.registry_raw_documents enable row level security;
revoke all on swift_v2.registry_raw_documents from public, anon, authenticated;

comment on table swift_v2.registry_raw_documents is
  '0001: redacted register documents, gzipped and deduplicated by content '
  'hash. Evaluation only — keep_until is real, prune it when the fields we '
  'need are settled.';

alter table swift_v2.source_handelsregister_records
    add column if not exists raw_document_sha256 text;

comment on column swift_v2.source_handelsregister_records.raw_document_sha256 is
  '0001: points at swift_v2.registry_raw_documents; null when nothing was kept.';


-- 4) The budget gate ---------------------------------------------------------
-- The worker calls this before EVERY request. If it returns granted=false it
-- must sleep wait_seconds and ask again; it must never decide for itself.
-- The config row is locked for the duration, so two processes cannot both be
-- granted the last slot in the window.

create or replace function swift_v2.registry_claim_request(
    p_source      text,
    p_kind        text,
    p_entity_id   uuid    default null,
    p_key         text    default null,
    p_run_id      text    default null)
returns jsonb
language plpgsql
security definer
set search_path to 'swift_v2', 'pg_catalog'
as $function$
declare
    c            swift_v2.registry_source_config%rowtype;
    v_now        timestamptz := now();
    v_hour       integer;
    v_used_hour  integer;
    v_used_day   integer;
    v_oldest_h   timestamptz;
    v_oldest_d   timestamptz;
    v_last       timestamptz;
    v_wait       numeric := 0;
    v_reason     text;
    v_id         uuid;
begin
    if p_kind not in ('session_open','search','document','other') then
        raise exception 'registry_claim_request: unknown request kind %', p_kind;
    end if;

    select * into c
      from swift_v2.registry_source_config
     where source = p_source
       for update;

    if not found then
        return jsonb_build_object('granted', false, 'reason', 'unknown_source',
                                  'wait_seconds', 0);
    end if;

    if not c.enabled then
        return jsonb_build_object('granted', false, 'reason', 'source_disabled',
                                  'wait_seconds', 0);
    end if;

    if c.cooldown_until is not null and c.cooldown_until > v_now then
        return jsonb_build_object(
            'granted', false, 'reason', 'circuit_open',
            'detail', c.cooldown_reason,
            'wait_seconds', ceil(extract(epoch from (c.cooldown_until - v_now))));
    end if;

    -- Allowed window (UTC). Outside it, say how long until it opens.
    v_hour := extract(hour from (v_now at time zone 'utc'))::integer;
    if v_hour < c.allowed_hour_start_utc or v_hour >= c.allowed_hour_end_utc then
        v_wait := extract(epoch from (
            (date_trunc('day', v_now at time zone 'utc')
             + make_interval(hours => c.allowed_hour_start_utc)
             + case when v_hour >= c.allowed_hour_end_utc then interval '1 day'
                    else interval '0' end) at time zone 'utc' - v_now));
        return jsonb_build_object('granted', false, 'reason', 'outside_allowed_hours',
                                  'wait_seconds', greatest(ceil(v_wait), 1));
    end if;

    select count(*), min(requested_at)
      into v_used_hour, v_oldest_h
      from swift_v2.registry_request_ledger
     where source = p_source and requested_at > v_now - interval '1 hour';

    select count(*), min(requested_at)
      into v_used_day, v_oldest_d
      from swift_v2.registry_request_ledger
     where source = p_source and requested_at > v_now - interval '1 day';

    select max(requested_at) into v_last
      from swift_v2.registry_request_ledger
     where source = p_source;

    if v_used_hour >= c.max_per_hour then
        v_reason := 'hour_budget_exhausted';
        v_wait   := 3600 - extract(epoch from (v_now - v_oldest_h));
    elsif v_used_day >= c.max_per_day then
        v_reason := 'day_budget_exhausted';
        v_wait   := 86400 - extract(epoch from (v_now - v_oldest_d));
    elsif v_last is not null
          and extract(epoch from (v_now - v_last)) < c.min_seconds_between then
        v_reason := 'min_gap';
        v_wait   := c.min_seconds_between - extract(epoch from (v_now - v_last));
    end if;

    if v_reason is not null then
        return jsonb_build_object(
            'granted', false, 'reason', v_reason,
            'wait_seconds', greatest(ceil(v_wait), 1),
            'used_hour', v_used_hour, 'max_per_hour', c.max_per_hour,
            'used_day', v_used_day,  'max_per_day', c.max_per_day);
    end if;

    insert into swift_v2.registry_request_ledger
        (source, request_kind, requested_at, run_id, entity_id, registry_identity_key)
    values (p_source, p_kind, v_now, p_run_id, p_entity_id, p_key)
    returning id into v_id;

    return jsonb_build_object(
        'granted', true, 'request_id', v_id,
        'used_hour', v_used_hour + 1, 'max_per_hour', c.max_per_hour,
        'used_day', v_used_day + 1,   'max_per_day', c.max_per_day,
        'remaining_hour', c.max_per_hour - v_used_hour - 1,
        'remaining_day',  c.max_per_day  - v_used_day  - 1);
end;
$function$;


create or replace function swift_v2.registry_finish_request(
    p_request_id  uuid,
    p_outcome     text,
    p_http_status integer default null,
    p_note        text    default null)
returns void
language sql
security definer
set search_path to 'swift_v2', 'pg_catalog'
as $function$
    update swift_v2.registry_request_ledger
       set outcome     = coalesce(p_outcome, outcome),
           http_status = p_http_status,
           note        = left(p_note, 500),
           finished_at = now()
     where id = p_request_id;
$function$;


-- The circuit breaker's durable half. A block, a 429 or a run of failures
-- parks the source for real: the next scheduled run finds the door shut
-- instead of walking into the same wall from the same IP.
create or replace function swift_v2.registry_open_circuit(
    p_source  text,
    p_reason  text,
    p_minutes integer default 360)
returns jsonb
language plpgsql
security definer
set search_path to 'swift_v2', 'pg_catalog'
as $function$
declare v_until timestamptz;
begin
    v_until := now() + make_interval(mins => greatest(p_minutes, 1));
    update swift_v2.registry_source_config
       set cooldown_until  = greatest(coalesce(cooldown_until, v_until), v_until),
           cooldown_reason = left(p_reason, 300),
           updated_at      = now(),
           updated_by      = 'circuit_breaker'
     where source = p_source
    returning cooldown_until into v_until;
    return jsonb_build_object('source', p_source, 'cooldown_until', v_until,
                              'reason', p_reason);
end;
$function$;


create or replace function swift_v2.registry_budget_status(p_source text default 'handelsregister')
returns jsonb
language sql
stable
security definer
set search_path to 'swift_v2', 'pg_catalog'
as $function$
    select jsonb_build_object(
        'source', c.source,
        'enabled', c.enabled,
        'whitelist_granted', c.whitelist_granted,
        'max_per_hour', c.max_per_hour,
        'max_per_day', c.max_per_day,
        'allowed_hours_utc', c.allowed_hour_start_utc || '-' || c.allowed_hour_end_utc,
        'cooldown_until', c.cooldown_until,
        'cooldown_reason', c.cooldown_reason,
        'used_last_hour', (select count(*) from swift_v2.registry_request_ledger l
                            where l.source = c.source and l.requested_at > now() - interval '1 hour'),
        'used_last_day',  (select count(*) from swift_v2.registry_request_ledger l
                            where l.source = c.source and l.requested_at > now() - interval '1 day'),
        'last_request_at', (select max(requested_at) from swift_v2.registry_request_ledger l
                             where l.source = c.source))
      from swift_v2.registry_source_config c
     where c.source = p_source;
$function$;


create or replace view swift_v2.v_registry_budget_status as
    select c.source,
           c.enabled,
           c.whitelist_granted,
           c.max_per_hour,
           c.max_per_day,
           c.cooldown_until,
           c.cooldown_reason,
           (select count(*) from swift_v2.registry_request_ledger l
             where l.source = c.source and l.requested_at > now() - interval '1 hour') as used_last_hour,
           (select count(*) from swift_v2.registry_request_ledger l
             where l.source = c.source and l.requested_at > now() - interval '1 day')  as used_last_day,
           (select count(*) from swift_v2.registry_request_ledger l
             where l.source = c.source and l.outcome = 'ok'
               and l.requested_at > now() - interval '1 day')                          as ok_last_day,
           (select max(requested_at) from swift_v2.registry_request_ledger l
             where l.source = c.source)                                                as last_request_at
      from swift_v2.registry_source_config c;


-- 5) Grants ------------------------------------------------------------------
revoke all on swift_v2.v_registry_budget_status from public, anon, authenticated;

revoke execute on function swift_v2.registry_claim_request(text,text,uuid,text,text)
    from public, anon, authenticated;
revoke execute on function swift_v2.registry_finish_request(uuid,text,integer,text)
    from public, anon, authenticated;
revoke execute on function swift_v2.registry_open_circuit(text,text,integer)
    from public, anon, authenticated;
revoke execute on function swift_v2.registry_budget_status(text)
    from public, anon, authenticated;

grant execute on function swift_v2.registry_claim_request(text,text,uuid,text,text) to service_role;
grant execute on function swift_v2.registry_finish_request(uuid,text,integer,text)  to service_role;
grant execute on function swift_v2.registry_open_circuit(text,text,integer)         to service_role;
grant execute on function swift_v2.registry_budget_status(text)                     to service_role;
grant select on swift_v2.v_registry_budget_status                                   to service_role;


-- 6) Self-checks -------------------------------------------------------------
do $$
declare v jsonb;
begin
    if has_function_privilege('anon','swift_v2.registry_claim_request(text,text,uuid,text,text)','EXECUTE')
       or has_function_privilege('authenticated','swift_v2.registry_claim_request(text,text,uuid,text,text)','EXECUTE') then
        raise exception '0001: the budget gate must be service_role-only';
    end if;

    -- The source ships disabled, so nothing can be granted by accident.
    v := swift_v2.registry_claim_request('handelsregister', 'search');
    if (v->>'granted')::boolean is not false or v->>'reason' <> 'source_disabled' then
        raise exception '0001: a disabled source must never grant a request: %', v;
    end if;

    -- The ceiling is a real wall, not a comment.
    begin
        update swift_v2.registry_source_config
           set max_per_hour = 60 where source = 'handelsregister';
        raise exception '0001: the ceiling check let 60/h through without a Whitelist';
    exception when check_violation then
        null;
    end;

    if (select count(*) from swift_v2.registry_request_ledger) <> 0 then
        raise exception '0001: the self-check must not leave ledger rows behind';
    end if;
end $$;
