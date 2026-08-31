# Reprodução da validação `20260831T033826Z`

Esta evidência pertence ao checkout cujo `HEAD` inicial está em `manifest.json`. Nenhum
valor secreto foi incluído. A chave OpenAI deve existir somente em `agent-service/.env`,
que permanece ignorado e fora do Git.

Os checks locais usam os executáveis do ambiente existente:

```bash
cd agent-service
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy --strict src/autoseguro
.venv/bin/pytest
.venv/bin/pip-audit
.venv/bin/alembic current
```

O runner live é reentrante por `RUN_ID` e recusa sobrescrever seus artifacts. Ele exige
PostgreSQL, Redis e quote-service isolados, além das variáveis não secretas apontando para
esses serviços:

```bash
VALIDATION_RUN_ID=NOVO_RUN_ID \
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/DB_ISOLADO \
REDIS_URL=redis://127.0.0.1:PORTA/0 \
QUOTE_SERVICE_URL=http://127.0.0.1:PORTA \
.venv/bin/python scripts/run_live_validation.py
```

Limites codificados: 20 tentativas HTTP, 30.000 tokens, USD 1,00 e 600 segundos de fase
paga. O runner usa structured output, `store=False`, tracing nativo do Agents SDK
desabilitado, nenhuma tool e nenhum handoff do SDK.

Resultados desta execução:

- OpenAIProvider: happy path `completed` com preço proveniente de uma tentativa HTTP 200.
- AgentsSDKProvider: `APIConnectionError` em um turno, fallback determinístico e handoff.
- Live total: 18 tentativas, 9.404 tokens e USD 0,0140915 estimados.
- Regressão final: 70 testes aprovados.
- Instabilidade seeded: 12 operações, sem amostra não seeded.
- Langfuse, Docker, CI remoto e Anthropic real não foram executados.

`pii-secret-scan-summary.json` registra um falso positivo inicial sobre o identificador de
revision Alembic e o rescan limpo após a correção. O relatório apresenta somente contagens
e categorias.
