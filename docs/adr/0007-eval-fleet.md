# ADR 0007: Eval-fleet offline determinística

## Status

Aceito para diagnóstico offline e shadow; fleet de LLM judges rejeitada neste escopo.

## Contexto

Integridade de preço, ausência de PII, coerência de handoff e retries deixam evidências estruturadas. Esses invariantes podem ser avaliados sem modelo. Uma fleet de LLM judges adicionaria variância, custo e risco de exportar contexto, e não havia credencial para calibrar sua contribuição real.

## Decisão

Quatro avaliadores side-effect-free processam somente `SanitizedTrace`: `QuoteIntegrityEvaluator`, `PrivacyEvaluator`, `HandoffEvaluator` e `ResilienceEvaluator`. Cada um produz `passed`, `score`, `findings`, `evidence_event_ids`, `confidence`, duração, tokens e custo. Eles executam em paralelo por `ThreadPoolExecutor`; a agregação, ordenação e score final são determinados por código.

O baseline contém verificações de integridade de cotação e privacidade. A fleet adiciona diagnóstico explícito para handoff e resiliência. Em dez traces sintéticas rotuladas, o baseline obteve concordância de 0,90, quatro falsos negativos e nenhum falso positivo; a fleet obteve concordância de 1,00, sem falsos positivos ou falsos negativos, encontrando quatro defeitos adicionais. O custo e os tokens foram zero. Esses números medem apenas o conjunto sintético versionado e não estimam desempenho em produção.

## Rejeição da fleet de agentes LLM

Nenhum avaliador foi implementado como Agent do OpenAI Agents SDK. A execução sobre traces live produziu um falso negativo: a fleet aprovou estruturalmente a trace do Agents SDK cujo happy path terminou em handoff. Não há evidência de ganho diagnóstico que justifique manter LLM judges. Se esse experimento for retomado, os especialistas não receberão tools, executarão offline ou em shadow sobre traces sanitizadas e serão comparados aos asserts determinísticos em concordância, falsos positivos, falsos negativos, custo, tokens e latência.

## Consequências

A eval-fleet não participa do atendimento e não altera estado. O termo fleet descreve a execução independente dos avaliadores, sem implicar uma arquitetura multi-agent no runtime. O relatório reproduzível fica em `artifacts/eval-fleet-report.json`.
