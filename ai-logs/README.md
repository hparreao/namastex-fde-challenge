# Transparência de uso de IA

Este projeto foi implementado com Codex. `codex-main-session-sanitized.jsonl` é o log
sanitizado da sessão principal de implementação intitulada “Clonar projeto Namastex FDE”,
thread ID `01a04a30-3839-7bb0-bb1e-93d7034659fa`. Ele não representa a totalidade das
interações com todas as ferramentas de IA; exports históricos auxiliares permanecem locais
e fora da superfície pública.

## Export sanitizado da sessão

`codex-main-session-sanitized.jsonl` é uma cópia derivada, nunca o arquivo original. A
origem permanece read-only sob `~/.codex/sessions/2026/08/28/`. O cutoff por número de
registros é capturado antes da chamada que gera o arquivo; por isso, o export termina no
turno anterior ao comando de exportação. Mensagens visíveis de user/assistant, planos,
tool calls e outputs existentes no formato `response_item` são preservados.

O script `sanitize_codex_session.py` exclui mensagens `developer`/`system`, reasoning,
conteúdo criptografado, token counts e metadados internos. Antes da escrita, mascara API
keys, bearer/capability tokens, senhas em URLs PostgreSQL, chaves privadas, e-mail, CPF,
telefone, CEP, placa e o nome do diretório pessoal. Identificadores UUID, trace ID e
revision Alembic são protegidos antes do masking. O primeiro registro e `manifest.json`
contêm o SHA-256 do prefixo da origem processado, período coberto e contagens. O arquivo
original não é modificado nem movido.

O export é um snapshot: deve ser regenerado ao término de uma sessão que continuou depois
da última geração. Depois da geração, execute Gitleaks e os testes de privacidade.

Regerar o snapshot:

```bash
wc -l ~/.codex/sessions/2026/08/28/rollout-2026-08-28T18-04-29-01a04a30-3839-7bb0-bb1e-93d7034659fa.jsonl
agent-service/.venv/bin/python ai-logs/sanitize_codex_session.py \
  ~/.codex/sessions/2026/08/28/rollout-2026-08-28T18-04-29-01a04a30-3839-7bb0-bb1e-93d7034659fa.jsonl \
  ai-logs/codex-main-session-sanitized.jsonl --max-records NUMERO_CAPTURADO
```
