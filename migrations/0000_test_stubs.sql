-- Stand-ins so migrations/0001 can be applied to an empty CI database.
-- NEVER run this against production: swift_v2 and these objects already exist
-- there, and this file deliberately knows nothing about their real shape.
create schema if not exists swift_v2;

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin bypassrls;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
end $$;

create table if not exists swift_v2.source_handelsregister_records (
    id                 uuid primary key default gen_random_uuid(),
    source_name        text not null,
    source_external_id text not null,
    entity_id          uuid,
    event_type         text,
    unique (source_name, source_external_id)
);
