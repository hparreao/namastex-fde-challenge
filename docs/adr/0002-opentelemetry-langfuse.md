# ADR 0002: OpenTelemetry com exportação OTLP opcional

- Status: aceito; exporter OTLP local validado, Langfuse operacional não executado
- Data: 2026-08-29

## Contexto

Logs e trace persistido permitiam reconstruir estado, mas não correlacionavam autorização,
decisão do modelo, policy e quote attempts como uma única execução por mensagem.

## Decisão

Uma boundary interna cria spans sanitizados e usa OpenTelemetry/OTLP quando habilitada.
O padrão é desligado e o backend no-op não adiciona dependência operacional. OTLP mantém o
destino intercambiável; Langfuse aceita ingestão OTLP, mas nenhuma instância foi adicionada
ao Compose principal.

O filtro client-side bloqueia atributos de prompt, resposta, conteúdo, secrets e capability
tokens. PII textual passa por masking antes do backend. Falhas de inicialização, início,
atributo, evento ou finalização são isoladas e não chegam ao lead.

## Consequências e evidência

Testes em memória cobrem a cadeia API, autorização, redaction, LLM, fallback, transição,
Cedar, quote attempt e completion, além da ausência de PII. Um backend que lança erro prova
que observabilidade indisponível não interrompe a aplicação. O exporter foi executado
contra receiver protobuf local e endpoint indisponível. Langfuse continua apenas como
compatibilidade arquitetural, pois nenhum backend Langfuse foi configurado.
