CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  trigger TEXT NOT NULL,
  mode TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 0,
  config_hash TEXT,
  git_sha TEXT,
  candidates_scanned INTEGER DEFAULT 0,
  candidates_researched INTEGER DEFAULT 0,
  positions_reviewed INTEGER DEFAULT 0,
  decisions INTEGER DEFAULT 0,
  orders_placed INTEGER DEFAULT 0,
  claude_calls INTEGER DEFAULT 0,
  claude_input_tokens INTEGER DEFAULT 0,
  claude_output_tokens INTEGER DEFAULT 0,
  claude_cost_usd TEXT DEFAULT '0',
  preflight_json TEXT,
  summary_json TEXT,
  error_text TEXT
);

CREATE TABLE IF NOT EXISTS markets (
  slug TEXT PRIMARY KEY,
  market_id TEXT,
  event_slug TEXT,
  question TEXT,
  title TEXT,
  description TEXT,
  category TEXT,
  market_type TEXT,
  start_date TEXT,
  end_date TEXT,
  game_start_time TEXT,
  status TEXT,
  active INTEGER,
  closed INTEGER,
  tick_size TEXT,
  fee_coefficient TEXT,
  min_qty TEXT,
  resolved_outcome TEXT,
  settlement_price TEXT,
  settled_at TEXT,
  no_trade_flag INTEGER DEFAULT 0,
  no_trade_reason TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS market_snapshots (
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL,
  ts TEXT NOT NULL,
  yes_bid TEXT,
  yes_ask TEXT,
  yes_bid_size TEXT,
  yes_ask_size TEXT,
  last_trade TEXT,
  open_interest TEXT,
  shares_traded TEXT,
  book_json TEXT,
  source TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_slug_ts ON market_snapshots(slug, ts);

CREATE TABLE IF NOT EXISTS candidates (
  candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  snapshot_id INTEGER,
  strategy TEXT NOT NULL,
  side TEXT,
  scan_score TEXT,
  rank INTEGER,
  researched INTEGER DEFAULT 0,
  skip_reason TEXT,
  features_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cand_run ON candidates(run_id);

CREATE TABLE IF NOT EXISTS research_notes (
  note_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  candidate_id INTEGER,
  slug TEXT NOT NULL,
  snapshot_id INTEGER,
  task TEXT NOT NULL,
  model TEXT,
  p_yes TEXT,
  p_yes_low TEXT,
  p_yes_high TEXT,
  confidence TEXT,
  event_already_occurred INTEGER,
  resolution_criteria_read INTEGER,
  resolution_ambiguity TEXT,
  evidence_json TEXT,
  key_risks_json TEXT,
  recommendation TEXT,
  summary TEXT,
  price_at_research TEXT,
  valid_until TEXT,
  raw_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_slug ON research_notes(slug, valid_until);

CREATE TABLE IF NOT EXISTS decisions (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  slug TEXT NOT NULL,
  note_id INTEGER,
  snapshot_id INTEGER,
  source TEXT NOT NULL,
  side TEXT,
  intent TEXT,
  p_model_raw TEXT,
  p_model_shrunk TEXT,
  p_market TEXT,
  best_bid TEXT,
  best_ask TEXT,
  spread TEXT,
  proposed_price TEXT,
  fee_per_contract TEXT,
  edge_net TEXT,
  kelly_full TEXT,
  kelly_used TEXT,
  tier TEXT,
  proposed_qty INTEGER,
  order_type TEXT,
  tif TEXT,
  expires_at TEXT,
  go INTEGER NOT NULL,
  nogo_reasons_json TEXT,
  rationale TEXT,
  equity_at_decision TEXT,
  exposure_at_decision TEXT,
  config_hash TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dec_run ON decisions(run_id);

CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE NOT NULL,
  exchange_order_id TEXT UNIQUE,
  decision_id INTEGER,
  run_id TEXT,
  slug TEXT NOT NULL,
  mode TEXT NOT NULL,
  source TEXT NOT NULL,
  intent TEXT NOT NULL,
  side TEXT NOT NULL,
  order_type TEXT NOT NULL,
  tif TEXT NOT NULL,
  post_only INTEGER DEFAULT 0,
  limit_price TEXT,
  qty INTEGER NOT NULL,
  filled_qty INTEGER DEFAULT 0,
  avg_fill_price TEXT,
  fees_paid TEXT DEFAULT '0',
  status TEXT NOT NULL,
  submitted_at TEXT,
  expires_at TEXT,
  closed_at TEXT,
  cancel_reason TEXT,
  preview_json TEXT,
  raw_response_json TEXT,
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_run_slug_intent ON orders(run_id, slug, intent) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS fills (
  fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT NOT NULL,
  exchange_fill_id TEXT UNIQUE,
  slug TEXT NOT NULL,
  ts TEXT NOT NULL,
  price TEXT NOT NULL,
  qty INTEGER NOT NULL,
  fee TEXT DEFAULT '0',
  liquidity TEXT,
  source TEXT
);

CREATE TABLE IF NOT EXISTS positions (
  slug TEXT NOT NULL,
  side TEXT NOT NULL,
  qty INTEGER NOT NULL,
  avg_cost TEXT,
  cost_basis TEXT,
  mark_price TEXT,
  unrealized_pnl TEXT,
  realized_pnl TEXT DEFAULT '0',
  status TEXT NOT NULL,
  exchange_qty INTEGER,
  discrepancy INTEGER DEFAULT 0,
  entry_decision_id INTEGER,
  opened_at TEXT,
  closed_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (slug, side)
);

CREATE TABLE IF NOT EXISTS settlements (
  settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL,
  side TEXT NOT NULL,
  resolved_outcome TEXT,
  settlement_price TEXT,
  resolved_at TEXT,
  detected_at TEXT NOT NULL,
  qty_held INTEGER,
  payout_total TEXT,
  cost_basis TEXT,
  fees_total TEXT,
  realized_pnl TEXT,
  won INTEGER,
  entry_decision_id INTEGER,
  p_model_at_entry TEXT,
  p_market_at_entry TEXT,
  days_held TEXT,
  UNIQUE(slug, side)
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  cash TEXT,
  reserved TEXT,
  positions_mark TEXT,
  equity TEXT,
  buying_power TEXT,
  source TEXT
);

CREATE TABLE IF NOT EXISTS paper_ledger (
  entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,
  amount TEXT NOT NULL,
  ref_table TEXT,
  ref_id TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
  date TEXT PRIMARY KEY,
  start_equity TEXT,
  end_equity TEXT,
  pnl TEXT,
  n_orders INTEGER,
  n_fills INTEGER,
  n_settled INTEGER,
  n_won INTEGER,
  fees TEXT,
  runs_ok INTEGER,
  runs_failed INTEGER,
  brier_model TEXT,
  brier_market TEXT,
  n_assessed INTEGER,
  claude_calls INTEGER,
  claude_tokens INTEGER,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS control (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS events_log (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT,
  level TEXT,
  event_type TEXT NOT NULL,
  ref_table TEXT,
  ref_id TEXT,
  payload_json TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
  notif_id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,
  dedupe_key TEXT UNIQUE,
  payload_json TEXT,
  attachment_path TEXT,
  created_at TEXT NOT NULL,
  posted_at TEXT,
  attempts INTEGER DEFAULT 0,
  error_text TEXT
);

CREATE TABLE IF NOT EXISTS claude_calls (
  call_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  task TEXT NOT NULL,
  slug TEXT,
  model TEXT,
  status TEXT NOT NULL,
  session_id TEXT,
  num_turns INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  cost_usd TEXT,
  duration_ms INTEGER,
  error_text TEXT,
  raw_path TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_log (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  user_id TEXT,
  user_name TEXT,
  text TEXT,
  reply TEXT,
  action TEXT,
  payload_json TEXT
);

CREATE TABLE IF NOT EXISTS calibration (
  category TEXT NOT NULL,
  price_band TEXT NOT NULL,
  n INTEGER NOT NULL,
  mean_residual TEXT,
  bias_est TEXT,
  updated_at TEXT,
  PRIMARY KEY (category, price_band)
);
