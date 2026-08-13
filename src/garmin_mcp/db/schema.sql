-- ══════════════════════════════════════════════════════════════════════
-- garmin-mcp — DuckDB schema
--
-- Design notes worth keeping in mind when editing:
--
--  * `records` deliberately has NO primary key. An ART index on a table that
--    grows by ~4 000 rows per activity would slow every insert for no
--    benefit; idempotency is enforced by DELETE-then-INSERT per activity.
--  * Wide tables, not EAV. DuckDB is columnar, so unused columns cost almost
--    nothing on disk and `SELECT heart_rate` reads exactly one column.
--  * `extra JSON` absorbs rare FIT fields so a new device never requires a
--    migration just to avoid losing data.
--  * Both a TIMESTAMPTZ (the truth, for joins) and a naive local TIMESTAMP
--    (what the athlete lived, for "my week of March 3rd") are stored.
-- ══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS schema_meta (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL
);

-- ─── Ingested files ───────────────────────────────────────────────────
-- Keyed by content hash so the same FIT arriving twice (once from the Garmin
-- sync, once dropped in the inbox by hand) is detected before parsing.
CREATE TABLE IF NOT EXISTS files (
    file_hash      VARCHAR PRIMARY KEY,   -- sha256 of the raw .fit bytes
    path           VARCHAR NOT NULL,      -- location under data/raw/
    source         VARCHAR NOT NULL,      -- 'garmin' | 'manual'
    bytes          BIGINT,
    downloaded_at  TIMESTAMPTZ,
    parsed_at      TIMESTAMPTZ,
    parser_version INTEGER NOT NULL,      -- bump to force a re-parse
    status         VARCHAR NOT NULL,      -- 'pending' | 'parsed' | 'failed'
    error          VARCHAR
);

-- ─── Activities ───────────────────────────────────────────────────────
-- One row per FIT `session` message. A multisport file (triathlon) yields one
-- aggregated parent row plus one child row per leg, including transitions,
-- linked by parent_activity_id.
CREATE TABLE IF NOT EXISTS activities (
    activity_id             BIGINT PRIMARY KEY,
    parent_activity_id      BIGINT,        -- NULL unless part of a multisport file
    session_index           INTEGER NOT NULL DEFAULT 0,
    source                  VARCHAR NOT NULL,
    file_hash               VARCHAR NOT NULL REFERENCES files(file_hash),
    garmin_activity_id      BIGINT,        -- NULL for manual imports

    sport                   VARCHAR,       -- running | cycling | swimming | transition | multisport
    sub_sport               VARCHAR,       -- trail | lap_swimming | indoor_cycling | ...
    name                    VARCHAR,

    start_time_utc          TIMESTAMPTZ NOT NULL,
    start_time_local        TIMESTAMP   NOT NULL,
    tz_offset_seconds       INTEGER,

    total_timer_time_s      DOUBLE,        -- moving time
    total_elapsed_time_s    DOUBLE,        -- wall-clock time
    total_distance_m        DOUBLE,
    total_ascent_m          DOUBLE,
    total_descent_m         DOUBLE,
    total_calories          INTEGER,

    avg_speed_mps           DOUBLE,
    max_speed_mps           DOUBLE,
    avg_heart_rate          SMALLINT,
    max_heart_rate          SMALLINT,
    avg_cadence             DOUBLE,        -- normalised: spm running, rpm cycling
    max_cadence             DOUBLE,
    avg_power_w             DOUBLE,
    max_power_w             DOUBLE,
    normalized_power_w      DOUBLE,
    intensity_factor        DOUBLE,
    training_stress_score   DOUBLE,
    aerobic_training_effect   DOUBLE,
    anaerobic_training_effect DOUBLE,
    avg_temperature_c       DOUBLE,

    pool_length_m           DOUBLE,        -- swimming only
    total_strokes           INTEGER,
    num_laps                INTEGER,

    device_product          VARCHAR,
    device_serial           BIGINT,
    start_lat               DOUBLE,
    start_lon               DOUBLE,

    ingested_at             TIMESTAMPTZ NOT NULL,
    extra                   JSON
);

-- ─── Laps ─────────────────────────────────────────────────────────────
-- Intervals live here. Without laps, "8 × 400 m" is indistinguishable from a
-- steady run in the summary numbers.
CREATE TABLE IF NOT EXISTS laps (
    activity_id        BIGINT  NOT NULL,
    lap_index          INTEGER NOT NULL,
    start_time_utc     TIMESTAMPTZ,
    total_timer_time_s DOUBLE,
    total_distance_m   DOUBLE,
    avg_speed_mps      DOUBLE,
    max_speed_mps      DOUBLE,
    avg_heart_rate     SMALLINT,
    max_heart_rate     SMALLINT,
    avg_cadence        DOUBLE,
    avg_power_w        DOUBLE,
    normalized_power_w DOUBLE,
    total_ascent_m     DOUBLE,
    total_descent_m    DOUBLE,
    total_calories     INTEGER,
    intensity          VARCHAR,   -- active | rest | warmup | cooldown
    lap_trigger        VARCHAR,
    -- Which prescribed step this lap belongs to, written by the watch when the
    -- session was run from a structured workout. Exact, so comparing what was
    -- asked against what happened needs no guesswork.
    wkt_step_index     INTEGER,
    PRIMARY KEY (activity_id, lap_index)
);

-- ─── The prescription ─────────────────────────────────────────────────
-- Present only for sessions started from a structured workout on the watch.
-- The plan travels inside the FIT file itself, so intent and execution can be
-- compared without linking anything back to a Garmin workout id.
CREATE TABLE IF NOT EXISTS workout_steps (
    activity_id      BIGINT  NOT NULL,
    step_index       INTEGER NOT NULL,
    workout_name     VARCHAR,
    intensity        VARCHAR,   -- warmup | active | recovery | cooldown | rest
    duration_type    VARCHAR,   -- time | distance | open | repeat_until_steps_cmplt
    duration_value   DOUBLE,    -- seconds or metres, per duration_type
    target_type      VARCHAR,   -- NULL when the step was left open (no alerts)
    target_low       DOUBLE,
    target_high      DOUBLE,
    repeat_from_step INTEGER,
    repeat_count     INTEGER,
    PRIMARY KEY (activity_id, step_index)
);

-- ─── Records ──────────────────────────────────────────────────────────
-- One sample per second. This is the big table: expect ~1M rows per year for
-- a triathlete. No primary key on purpose (see header).
CREATE TABLE IF NOT EXISTS records (
    activity_id             BIGINT      NOT NULL,
    ts                      TIMESTAMPTZ NOT NULL,
    elapsed_s               DOUBLE      NOT NULL,  -- precomputed: ts - session start
    lap_index               INTEGER,

    lat                     DOUBLE,                -- semicircles already converted
    lon                     DOUBLE,
    altitude_m              DOUBLE,
    distance_m              DOUBLE,
    speed_mps               DOUBLE,
    heart_rate              SMALLINT,
    cadence                 SMALLINT,
    power_w                 SMALLINT,
    temperature_c           SMALLINT,
    grade                   DOUBLE,

    vertical_oscillation_mm DOUBLE,
    vertical_ratio          DOUBLE,
    stance_time_ms          DOUBLE,
    stance_time_percent     DOUBLE,
    step_length_mm          DOUBLE,
    left_right_balance      DOUBLE,
    respiration_rate        DOUBLE,
    accumulated_power_w     BIGINT,

    extra                   JSON
);

CREATE INDEX IF NOT EXISTS idx_records_activity_ts ON records (activity_id, ts);
CREATE INDEX IF NOT EXISTS idx_activities_start    ON activities (start_time_local);
CREATE INDEX IF NOT EXISTS idx_activities_parent   ON activities (parent_activity_id);
CREATE INDEX IF NOT EXISTS idx_activities_sport    ON activities (sport);

-- ─── Convenience view ─────────────────────────────────────────────────
-- Derived values computed once here rather than repeated in every query.
-- `week_start` uses DuckDB's Monday-based week truncation, matching the ISO
-- weeks athletes plan in.
CREATE OR REPLACE VIEW v_activity_summary AS
SELECT
    a.*,
    CASE WHEN a.avg_speed_mps > 0 THEN 1000.0 / a.avg_speed_mps END AS pace_s_per_km,
    CASE WHEN a.avg_speed_mps > 0 THEN a.avg_speed_mps * 3.6      END AS avg_speed_kmh,
    CAST(date_trunc('week', a.start_time_local) AS DATE)             AS week_start,
    EXTRACT(isoyear FROM a.start_time_local)                         AS iso_year,
    EXTRACT(week    FROM a.start_time_local)                         AS iso_week,
    CAST(a.start_time_local AS DATE)                                 AS local_date,
    (a.parent_activity_id IS NOT NULL)                               AS is_child_session
FROM activities a;
