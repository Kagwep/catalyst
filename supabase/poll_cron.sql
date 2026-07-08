-- Exact 15-minute poll cadence: pg_cron → poll-dispatch Edge Function →
-- GitHub workflow_dispatch. Run this in the Supabase SQL editor AFTER the
-- function is deployed and its secrets are set (see HOSTING.md §5).
--
-- Replace <DISPATCH_SECRET> below with the same value you stored as the
-- function's DISPATCH_SECRET secret.

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- Re-running this file replaces the job (unschedule is a no-op if absent).
do $$
begin
  perform cron.unschedule('poll-dispatch-15m');
exception when others then null;
end $$;

select cron.schedule(
  'poll-dispatch-15m',
  '*/15 * * * *',
  $$
  select net.http_post(
    url := 'https://tqkwztozcoyekpacwqnq.supabase.co/functions/v1/poll-dispatch',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-dispatch-secret', '<DISPATCH_SECRET>'
    ),
    body := '{}'::jsonb,
    timeout_milliseconds := 10000
  );
  $$
);

-- Verify the job exists:
--   select jobid, jobname, schedule, active from cron.job;
-- Watch it fire (pg_net keeps recent responses; expect status 200 and
-- {"dispatched": true, ...} in the body):
--   select id, status_code, content::text, created
--   from net._http_response order by id desc limit 5;
-- Stop it:
--   select cron.unschedule('poll-dispatch-15m');
