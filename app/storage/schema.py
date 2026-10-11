"""Versioned SQLite schema, introduced incrementally with implementation steps."""

SCHEMA_VERSION = 4

SCHEMA_V1 = (
    """
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        subscription_id TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('preview', 'scheduled')),
        status TEXT NOT NULL CHECK (status IN (
            'queued', 'running', 'waiting_retry', 'completed', 'no_news', 'failed'
        )),
        subscription_json TEXT NOT NULL,
        budget_limits_json TEXT NOT NULL,
        budget_usage_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        scheduled_for TEXT,
        window_start TEXT NOT NULL,
        window_end TEXT NOT NULL,
        started_at TEXT,
        deadline_at TEXT,
        retry_at TEXT,
        stop_reason TEXT,
        CHECK (window_start < window_end),
        CHECK ((mode = 'scheduled') = (scheduled_for IS NOT NULL)),
        CHECK (scheduled_for IS NULL OR scheduled_for = window_end),
        CHECK ((started_at IS NULL) = (deadline_at IS NULL)),
        CHECK (status != 'queued' OR started_at IS NULL),
        CHECK (status IN ('queued', 'failed') OR started_at IS NOT NULL),
        CHECK ((status = 'waiting_retry') = (retry_at IS NOT NULL)),
        CHECK (status != 'failed' OR stop_reason IS NOT NULL)
    )
    """,
    "CREATE INDEX runs_user_created ON runs(user_id, created_at, id)",
    "CREATE INDEX runs_status_retry ON runs(status, retry_at)",
    """
    CREATE TABLE run_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        data_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX run_events_timeline ON run_events(run_id, id)",
)

SCHEMA_V2 = (
    """
    CREATE TABLE tool_calls (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL,
        name TEXT NOT NULL,
        arguments TEXT NOT NULL,
        invocation_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'executing', 'completed')),
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        result_json TEXT,
        PRIMARY KEY (run_id, tool_call_id),
        CHECK ((status = 'completed') = (result_json IS NOT NULL)),
        CHECK ((status = 'completed') = (finished_at IS NOT NULL)),
        CHECK (status != 'executing' OR started_at IS NOT NULL)
    )
    """,
    "CREATE INDEX tool_calls_pending ON tool_calls(run_id, status, created_at)",
)

SCHEMA_V3 = (
    """
    CREATE TABLE model_responses (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        turn INTEGER NOT NULL CHECK (turn > 0),
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, turn)
    )
    """,
    """
    CREATE TABLE model_tool_calls (
        run_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        position INTEGER NOT NULL CHECK (position >= 0),
        tool_call_id TEXT NOT NULL,
        PRIMARY KEY (run_id, turn, position),
        UNIQUE (run_id, tool_call_id),
        FOREIGN KEY (run_id, turn) REFERENCES model_responses(run_id, turn) ON DELETE CASCADE,
        FOREIGN KEY (run_id, tool_call_id)
            REFERENCES tool_calls(run_id, tool_call_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE run_messages (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL CHECK (seq > 0),
        role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
        purpose TEXT CHECK (purpose IN ('initial', 'missing_submission')),
        response_turn INTEGER,
        tool_call_id TEXT,
        message_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, seq),
        FOREIGN KEY (run_id, response_turn) REFERENCES model_responses(run_id, turn),
        FOREIGN KEY (run_id, tool_call_id) REFERENCES tool_calls(run_id, tool_call_id),
        CHECK ((role = 'tool') = (tool_call_id IS NOT NULL)),
        CHECK (role NOT IN ('assistant', 'tool') OR response_turn IS NOT NULL),
        CHECK (purpose IS NULL OR role IN ('system', 'user'))
    )
    """,
    """CREATE UNIQUE INDEX tool_feedback_once ON run_messages(run_id, tool_call_id)
        WHERE role = 'tool'""",
    """CREATE UNIQUE INDEX assistant_response_once ON run_messages(run_id, response_turn)
        WHERE role = 'assistant'""",
    """CREATE UNIQUE INDEX missing_submission_once ON run_messages(run_id, purpose)
        WHERE purpose = 'missing_submission'""",
)

SCHEMA_V4 = (
    """
    CREATE TABLE external_requests (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('model', 'tool')),
        tool_call_id TEXT,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ready', 'executing', 'waiting', 'completed')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
        max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 3),
        retry_base_seconds REAL NOT NULL CHECK (retry_base_seconds > 0),
        retry_at TEXT,
        failure_json TEXT,
        feedback_json TEXT,
        stop_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (run_id, tool_call_id)
            REFERENCES tool_calls(run_id, tool_call_id) ON DELETE CASCADE,
        CHECK ((kind = 'tool') = (tool_call_id IS NOT NULL)),
        CHECK ((status = 'waiting') = (retry_at IS NOT NULL)),
        CHECK (attempts <= max_attempts)
    )
    """,
    """CREATE UNIQUE INDEX active_model_request ON external_requests(run_id)
        WHERE kind = 'model' AND status != 'completed'""",
    """CREATE UNIQUE INDEX tool_request_once ON external_requests(run_id, tool_call_id)
        WHERE kind = 'tool'""",
    "ALTER TABLE model_responses ADD COLUMN request_id TEXT REFERENCES external_requests(id)",
    "CREATE UNIQUE INDEX response_request_once ON model_responses(request_id)",
    "ALTER TABLE run_messages RENAME TO run_messages_v3",
    """
    CREATE TABLE run_messages (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL CHECK (seq > 0),
        role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
        purpose TEXT CHECK (purpose IN (
            'initial', 'missing_submission', 'budget_warning', 'repeat_warning', 'retry_notice'
        )),
        response_turn INTEGER,
        tool_call_id TEXT,
        message_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, seq),
        FOREIGN KEY (run_id, response_turn) REFERENCES model_responses(run_id, turn),
        FOREIGN KEY (run_id, tool_call_id) REFERENCES tool_calls(run_id, tool_call_id),
        CHECK ((role = 'tool') = (tool_call_id IS NOT NULL)),
        CHECK (role NOT IN ('assistant', 'tool') OR response_turn IS NOT NULL),
        CHECK (purpose IS NULL OR role IN ('system', 'user'))
    )
    """,
    "INSERT INTO run_messages SELECT * FROM run_messages_v3",
    "DROP TABLE run_messages_v3",
    """CREATE UNIQUE INDEX tool_feedback_once ON run_messages(run_id, tool_call_id)
        WHERE role = 'tool'""",
    """CREATE UNIQUE INDEX assistant_response_once ON run_messages(run_id, response_turn)
        WHERE role = 'assistant'""",
    """CREATE UNIQUE INDEX missing_submission_once ON run_messages(run_id, purpose)
        WHERE purpose = 'missing_submission'""",
    """
    CREATE TABLE harness_notices (
        run_id TEXT NOT NULL,
        notice_key TEXT NOT NULL,
        message_seq INTEGER NOT NULL,
        PRIMARY KEY (run_id, notice_key),
        FOREIGN KEY (run_id, message_seq) REFERENCES run_messages(run_id, seq) ON DELETE CASCADE
    )
    """,
)
