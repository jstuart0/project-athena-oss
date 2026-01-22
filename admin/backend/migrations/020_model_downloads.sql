-- Migration: Model Downloads
-- Description: Add table for tracking Hugging Face model downloads
-- Date: 2026-01-05

-- Model downloads table for HF Hub integration
CREATE TABLE IF NOT EXISTS model_downloads (
    id SERIAL PRIMARY KEY,

    -- Model identification
    repo_id VARCHAR(255) NOT NULL,           -- "TheBloke/Llama-2-7B-GGUF"
    filename VARCHAR(255) NOT NULL,          -- "llama-2-7b.Q4_K_M.gguf"
    model_format VARCHAR(32) NOT NULL,       -- "gguf", "mlx"
    quantization VARCHAR(32),                -- "Q4_K_M", "Q8_0", etc.

    -- Download metadata
    file_size_bytes BIGINT,
    download_path TEXT,

    -- Status tracking
    status VARCHAR(32) NOT NULL DEFAULT 'pending',  -- pending, downloading, processing, completed, failed, cancelled
    progress_percent FLOAT DEFAULT 0,
    downloaded_bytes BIGINT DEFAULT 0,
    error_message TEXT,

    -- Ollama integration
    ollama_model_name VARCHAR(255),
    ollama_imported BOOLEAN DEFAULT FALSE,

    -- Audit
    created_by_id INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,

    UNIQUE(repo_id, filename)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_model_downloads_status ON model_downloads(status);
CREATE INDEX IF NOT EXISTS idx_model_downloads_created_at ON model_downloads(created_at);
CREATE INDEX IF NOT EXISTS idx_model_downloads_format ON model_downloads(model_format);
