# ADR 0005: Não adicionar pgvector

- Status: rejeitado
- Data: 2026-08-29

## Contexto

pgvector só teria função clara se retrieval de exemplos sanitizados melhorasse um eval
offline de objeções ou extração. O dataset sintético existente mede regexes conhecidas e
não oferece baseline representativo para provar esse ganho.

## Decisão

Não instalar a extensão, criar índices ou operar um segundo vector store. Redis permanece
restrito à coordenação e não é usado como vector database. Essa separação evita redundância
e custo operacional sem hipótese confirmada.

## Critério para reconsideração

Reabrir apenas com corpus sanitizado e rotulado, baseline sem retrieval, métrica definida
por campo/intenção, comparação de qualidade e latência, e ganho que compense armazenamento,
indexação e governança dos exemplos recuperados.
