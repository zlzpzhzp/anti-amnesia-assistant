-- Minimal "store" — the structured, queryable place the agent files things on your command.
-- SQLite here for zero-setup; swap for Postgres/Supabase/Notion/a calendar API as you grow.
-- The point is a STABLE SCHEMA (so the agent files consistently) + READ-BACK (you + memory review).
--
--   sqlite3 store.db < schema.sql
--   # then the agent inserts on your "register it" command, and reads on "what's on for Tuesday?"

CREATE TABLE IF NOT EXISTS contacts (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  phone      TEXT,
  note       TEXT,                      -- "parent of X", "ACME accounts-payable", ...
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (          -- appointments / schedule
  id         INTEGER PRIMARY KEY,
  title      TEXT NOT NULL,
  date       TEXT NOT NULL,             -- YYYY-MM-DD (validate before insert)
  time       TEXT,                      -- HH:MM, nullable (all-day)
  kind       TEXT,                      -- meeting / call / personal / ...
  contact_id INTEGER REFERENCES contacts(id),
  source     TEXT,                      -- which item created it: sms|call|voice|email
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
  id         INTEGER PRIMARY KEY,
  title      TEXT NOT NULL,
  due        TEXT,                      -- YYYY-MM-DD, nullable
  done       INTEGER DEFAULT 0,
  source     TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notes (           -- free-form captures the agent files
  id         INTEGER PRIMARY KEY,
  body       TEXT NOT NULL,
  tags       TEXT,                      -- comma-separated or JSON
  source     TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_date   ON events(date);
CREATE INDEX IF NOT EXISTS idx_tasks_due     ON tasks(due) WHERE done = 0;
CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name);

-- NOTE: this store holds personal data. Lock the file (chmod 600), keep it off git
-- (it's in .gitignore), and back it up. See the README "Security" section.
