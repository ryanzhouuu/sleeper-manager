CREATE TABLE IF NOT EXISTS forecast_capture_contexts (
    receipt_id TEXT PRIMARY KEY,
    encoding TEXT NOT NULL CHECK (encoding IN ('gzip_json')),
    encoded_payload BLOB NOT NULL CHECK (length(encoded_payload) > 0),
    uncompressed_size INTEGER NOT NULL CHECK (uncompressed_size > 0),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    stored_at TEXT NOT NULL,
    FOREIGN KEY (receipt_id) REFERENCES forecast_fetch_receipts(receipt_id)
);

CREATE INDEX IF NOT EXISTS forecast_capture_contexts_stored_idx
ON forecast_capture_contexts (stored_at, receipt_id);
