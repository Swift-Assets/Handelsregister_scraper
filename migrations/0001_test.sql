-- Behavioural test for 0001. Run against a THROWAWAY database only:
--   psql -v ON_ERROR_STOP=1 -f migrations/0001_registry_budget_and_ledger.sql
--   psql -v ON_ERROR_STOP=1 -f migrations/0001_test.sql
-- It asserts with raise exception, so a silent pass is a real pass.

do $$
declare
    v jsonb;
    v_hour integer := extract(hour from (now() at time zone 'utc'))::integer;
begin
    delete from swift_v2.registry_request_ledger;
    update swift_v2.registry_source_config
       set enabled = true, max_per_hour = 3, max_per_day = 4,
           min_seconds_between = 0, cooldown_until = null, cooldown_reason = null,
           allowed_hour_start_utc = 0, allowed_hour_end_utc = 24
     where source = 'handelsregister';

    -- 1) a plain grant
    v := swift_v2.registry_claim_request('handelsregister', 'search');
    if (v->>'granted')::boolean is not true then
        raise exception 'T1 a healthy source must grant: %', v;
    end if;
    if (v->>'remaining_hour')::int <> 2 then
        raise exception 'T1 remaining_hour should be 2, got %', v;
    end if;

    -- 2) the grant is the ledger row; a claim that is never used still costs
    if (select count(*) from swift_v2.registry_request_ledger) <> 1 then
        raise exception 'T2 a grant must write its ledger row';
    end if;

    -- 3) the hourly cap is a wall
    perform swift_v2.registry_claim_request('handelsregister', 'search');
    perform swift_v2.registry_claim_request('handelsregister', 'document');
    v := swift_v2.registry_claim_request('handelsregister', 'search');
    if (v->>'granted')::boolean is not false or v->>'reason' <> 'hour_budget_exhausted' then
        raise exception 'T3 the 4th request in an hour of 3 must be refused: %', v;
    end if;
    if (v->>'wait_seconds')::int <= 0 then
        raise exception 'T3 a refusal must say how long to wait: %', v;
    end if;

    -- 4) the daily cap bites even when the hour has room
    update swift_v2.registry_request_ledger
       set requested_at = now() - interval '90 minutes';
    v := swift_v2.registry_claim_request('handelsregister', 'search');   -- 4th today
    if (v->>'granted')::boolean is not true then
        raise exception 'T4 an aged-out hour must free the hourly budget: %', v;
    end if;
    v := swift_v2.registry_claim_request('handelsregister', 'search');   -- 5th today
    if (v->>'granted')::boolean is not false or v->>'reason' <> 'day_budget_exhausted' then
        raise exception 'T4 the daily cap must hold when the hour has room: %', v;
    end if;

    -- 5) the politeness floor
    delete from swift_v2.registry_request_ledger;
    update swift_v2.registry_source_config
       set min_seconds_between = 45 where source = 'handelsregister';
    perform swift_v2.registry_claim_request('handelsregister', 'search');
    v := swift_v2.registry_claim_request('handelsregister', 'document');
    if v->>'reason' <> 'min_gap' then
        raise exception 'T5 two requests back to back must be spaced: %', v;
    end if;

    -- 6) outside the allowed window
    delete from swift_v2.registry_request_ledger;
    update swift_v2.registry_source_config
       set min_seconds_between = 0,
           allowed_hour_start_utc = ((v_hour + 2) % 22) + 1,
           allowed_hour_end_utc   = ((v_hour + 2) % 22) + 2
     where source = 'handelsregister';
    v := swift_v2.registry_claim_request('handelsregister', 'search');
    if v->>'reason' <> 'outside_allowed_hours' then
        raise exception 'T6 the portal must be left alone outside its window: %', v;
    end if;
    if (v->>'wait_seconds')::int <= 0 then
        raise exception 'T6 must say when the window opens: %', v;
    end if;
    if (select count(*) from swift_v2.registry_request_ledger) <> 0 then
        raise exception 'T6 a refusal must not spend budget';
    end if;

    -- 7) an open circuit outranks everything
    update swift_v2.registry_source_config
       set allowed_hour_start_utc = 0, allowed_hour_end_utc = 24
     where source = 'handelsregister';
    perform swift_v2.registry_open_circuit('handelsregister', 'ip_blocked', 1440);
    v := swift_v2.registry_claim_request('handelsregister', 'search');
    if v->>'reason' <> 'circuit_open' or v->>'detail' <> 'ip_blocked' then
        raise exception 'T7 a parked source must grant nothing: %', v;
    end if;

    -- 8) a cooldown is extended, never shortened
    perform swift_v2.registry_open_circuit('handelsregister', 'portal_error_page', 1);
    if (select cooldown_until from swift_v2.registry_source_config
         where source = 'handelsregister') < now() + interval '23 hours' then
        raise exception 'T8 a shorter cooldown must not cut a longer one short';
    end if;

    -- 9) disabling stops everything
    update swift_v2.registry_source_config
       set cooldown_until = null, enabled = false where source = 'handelsregister';
    v := swift_v2.registry_claim_request('handelsregister', 'search');
    if v->>'reason' <> 'source_disabled' then
        raise exception 'T9 a disabled source must grant nothing: %', v;
    end if;

    -- 10) an unknown source is refused, not created
    v := swift_v2.registry_claim_request('does-not-exist', 'search');
    if v->>'reason' <> 'unknown_source' then
        raise exception 'T10 unknown sources must be refused: %', v;
    end if;

    -- 11) the ceilings
    update swift_v2.registry_source_config
       set enabled = true, max_per_hour = 55, max_per_day = 900
     where source = 'handelsregister';                       -- allowed: at the line
    begin
        update swift_v2.registry_source_config set max_per_hour = 56
         where source = 'handelsregister';
        raise exception 'T11 56/h without a Whitelist must be refused';
    exception when check_violation then null; end;
    begin
        update swift_v2.registry_source_config set max_per_day = 901
         where source = 'handelsregister';
        raise exception 'T11 901/day without a Whitelist must be refused';
    exception when check_violation then null; end;

    -- 12) a granted Whitelist raises the ceiling to what we applied for, no further
    update swift_v2.registry_source_config
       set whitelist_granted = true, max_per_hour = 150, max_per_day = 1000,
           whitelist_note = 'test' where source = 'handelsregister';
    begin
        update swift_v2.registry_source_config set max_per_hour = 151
         where source = 'handelsregister';
        raise exception 'T12 151/h must be refused even with a Whitelist';
    exception when check_violation then null; end;

    -- 13) an unknown request kind is a programming error, not a silent 'other'
    begin
        v := swift_v2.registry_claim_request('handelsregister', 'scrape-everything');
        raise exception 'T13 an unknown request kind must raise';
    exception when others then
        if sqlerrm not like '%unknown request kind%' then raise; end if;
    end;

    -- 14) finish() records the outcome against the right row
    delete from swift_v2.registry_request_ledger;
    update swift_v2.registry_source_config
       set whitelist_granted = false, max_per_hour = 40, max_per_day = 700
     where source = 'handelsregister';
    v := swift_v2.registry_claim_request('handelsregister', 'document', null, 'k1', 'run-x');
    perform swift_v2.registry_finish_request((v->>'request_id')::uuid, 'ok', 200, 'note');
    if (select outcome from swift_v2.registry_request_ledger
         where id = (v->>'request_id')::uuid) <> 'ok' then
        raise exception 'T14 finish must record the outcome';
    end if;
    if (select run_id from swift_v2.registry_request_ledger
         where id = (v->>'request_id')::uuid) <> 'run-x' then
        raise exception 'T14 the run id must survive';
    end if;

    -- 15) the status view counts what the ledger holds
    if (swift_v2.registry_budget_status('handelsregister')->>'used_last_hour')::int <> 1 then
        raise exception 'T15 status must report the real usage';
    end if;

    delete from swift_v2.registry_request_ledger;
    update swift_v2.registry_source_config
       set enabled = false, min_seconds_between = 45,
           allowed_hour_start_utc = 5, allowed_hour_end_utc = 22
     where source = 'handelsregister';
    raise notice 'ALL 15 BUDGET TESTS PASSED';
end $$;
