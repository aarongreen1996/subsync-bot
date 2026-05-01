-- Run this in your Supabase SQL Editor to create the table
-- Go to: supabase.com → your project → SQL Editor → New query → paste this → Run

create table site_logs (
  id            bigint generated always as identity primary key,
  from_number   text,
  raw_message   text,
  type          text,          -- VARIATION | DAYWORK | MATERIAL_ORDER | TIMESHEET | UNKNOWN
  description   text,
  hours         numeric,
  cost_estimate numeric,
  location      text,
  requested_by  text,
  worker_name   text,
  materials     text,          -- stored as JSON string
  supplier      text,
  quantity      text,
  status        text default 'pending',   -- pending | reviewed | sent
  created_at    timestamptz default now()
);

-- Optional: enable row-level security (recommended for production)
-- alter table site_logs enable row level security;
