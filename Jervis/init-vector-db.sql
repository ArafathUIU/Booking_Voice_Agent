CREATE EXTENSION IF NOT EXISTS vector;

DROP TABLE IF EXISTS conversation_summaries CASCADE;
DROP TABLE IF EXISTS knowledge_chunks CASCADE;

CREATE TABLE knowledge_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    category VARCHAR(100) NOT NULL,
    title VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    embedding vector(384),
    chunk_metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_knowledge_chunks_embedding
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX idx_knowledge_chunks_tenant_category
    ON knowledge_chunks (tenant_id, category);

CREATE TABLE conversation_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    summary TEXT NOT NULL,
    embedding vector(384),
    chunk_metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_conversation_summaries_embedding
    ON conversation_summaries USING hnsw (embedding vector_cosine_ops);