create table if not exists agents (
  id    text primary key,
  name  text not null,
  role  text not null,
  phase int  not null default 1,
  seq   int  not null default 0
);

create table if not exists channels (
  id            serial primary key,
  slug          text unique not null,
  name          text not null,
  language      text not null default 'en',
  niche         text,
  yt_channel_id text,
  active        boolean not null default true,
  created_at    timestamptz not null default now()
);

create table if not exists videos (
  id         bigserial primary key,
  channel_id int references channels(id) on delete cascade,
  kind       text not null check (kind in ('long','short')),
  title      text,
  status     text not null default 'draft',
  script     jsonb,
  file_path  text,
  duration_s int,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists agent_runs (
  id          bigserial primary key,
  agent_id    text references agents(id) on delete cascade,
  video_id    bigint references videos(id) on delete cascade,
  status      text not null,
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  duration_s  numeric,
  note        text,
  payload     jsonb
);
create index if not exists idx_agent_runs_latest on agent_runs(agent_id, id desc);

create table if not exists approvals (
  id         bigserial primary key,
  video_id   bigint references videos(id) on delete cascade,
  chat_id    bigint,
  message_id bigint,
  state      text not null default 'pending',
  decided_at timestamptz,
  created_at timestamptz not null default now()
);

alter table videos add column if not exists yt_video_id text;
alter table videos add column if not exists published_at timestamptz;
alter table videos add column if not exists parent_id bigint references videos(id);

create table if not exists channel_daily (
  channel_id  int references channels(id) on delete cascade,
  day         date not null,
  views       bigint default 0,
  watch_min   numeric default 0,
  subs_gained int default 0,
  subs_lost   int default 0,
  primary key (channel_id, day)
);

create table if not exists metrics_daily (
  yt_video_id  text not null,
  day          date not null,
  views        bigint default 0,
  watch_min    numeric default 0,
  avg_view_pct numeric default 0,
  subs_gained  int default 0,
  likes        int default 0,
  comments     int default 0,
  shares       int default 0,
  primary key (yt_video_id, day)
);

create table if not exists trend_reports (
  channel_id     int references channels(id) on delete cascade,
  day            date not null,
  videos_scanned int,
  report         jsonb,
  primary key (channel_id, day)
);

create table if not exists news_picks (
  channel_id int references channels(id) on delete cascade,
  day        date not null,
  pick       jsonb,
  used       boolean not null default false,
  primary key (channel_id, day)
);

create table if not exists incidents (
  id        bigserial primary key,
  ts        timestamptz not null default now(),
  agent     text,
  category  text,
  error     text,
  action    text,
  resolved  boolean not null default false
);
