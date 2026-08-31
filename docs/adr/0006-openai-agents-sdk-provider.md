# ADR 0006: OpenAI Agents SDK como provider opcional

## Status

Aceito como adapter opcional; execução real apresentou falha de confiabilidade no happy path.

## Contexto

O fluxo já possui `LLMProvider` e uma state machine que controla transições, retries, cotação, preço, handoff e persistência. Migrar a orquestração para o SDK aumentaria a superfície de decisão probabilística sem resolver uma lacuna do domínio.

## Decisão

`AgentsSDKProvider` usa um único `Agent`, `output_type=AgentDecision`, Responses API e no máximo um turno. O Agent não recebe tools nem handoffs. Portanto, não pode executar `/quote`, persistir dados, calcular preço ou alterar estado. O adapter usa a mesma interface dos providers existentes e a state machine continua validando e aplicando a decisão.

O tracing nativo do Agents SDK permanece desabilitado. A aplicação já cria um span `llm_decision` sanitizado no OpenTelemetry e associa a mensagem a um `canonical_trace_id`. O mesmo ID é passado ao `RunConfig`, com `trace_include_sensitive_data=False`, mas não é exportado pelo SDK. Essa decisão evita spans duplicados e mantém um único exportador sob masking client-side.

Handoff humano permanece um estado de negócio da aplicação. Handoffs do SDK não são usados. Se um especialista artificial delimitado vier a ser testado, o padrão preferencial será `Agent.as_tool()` sob um manager, mantendo a resposta final e as autorizações no chamador.

## Evidência e limites

Os testes locais verificam o schema estruturado, ausência de tools/handoffs, limite de um turno, tracing desabilitado, trace ID canônico e coleta de usage. O pacote validado localmente foi `openai-agents==0.22.0`. Na campanha `20260831T033826Z`, o provider executou chamadas reais com structured output. Um `APIConnectionError` sem status em um turno acionou fallback e levou o fluxo a handoff. O adapter é compatível com a API, mas não obteve equivalência funcional E2E com o provider direto nesta amostra.

## Consequências

A dependência fica em um extra opcional `agents`. O provider direto da Responses API continua disponível e é arquiteturalmente mais simples para este caso. A adoção como padrão exige comparação real de decisões, tokens, custo e latência com credencial e dataset representativo.
