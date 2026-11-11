CREATE TABLE passkey_account (
 id INTEGER PRIMARY KEY CHECK(id=1), user_handle BLOB NOT NULL UNIQUE
);
CREATE TABLE passkeys (
 id INTEGER PRIMARY KEY,
 credential_id BLOB NOT NULL UNIQUE,
 public_key BLOB NOT NULL,
 sign_count INTEGER NOT NULL DEFAULT 0,
 rp_id TEXT NOT NULL,
 name TEXT NOT NULL,
 transports TEXT NOT NULL DEFAULT '[]',
 device_type TEXT NOT NULL,
 backed_up INTEGER NOT NULL DEFAULT 0,
 created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 last_used TEXT
);
CREATE INDEX passkeys_rp ON passkeys(rp_id);
INSERT INTO schema_migrations(version) VALUES(4);
