# ADR 0004: Rejeitar semantic cache neste escopo

- Status: rejeitado
- Data: 2026-08-29

## Contexto

Cachear `AgentDecision` poderia reduzir chamadas e latência, mas similaridade alta não
implica equivalência segura. Estado, dados coletados, provider, modelo e versões reduzem
colisões de contexto, sem resolver negação ou mudanças pequenas no mesmo namespace.

## Experimento e decisão

O conjunto adversarial contém 15 pares sobre estado, plano, confirmação, dados e negação,
incluindo controles seguros. Um proxy lexical com threshold 0,82 produziu 10 unsafe hits em
15 hits sem namespace, taxa 0,6667. O namespace completo reduziu colisões entre contextos,
mas manteve 7 unsafe hits em 12 hits, taxa 0,5833.

Sem embedding model validado e corpus real rotulado, implementar cache ampliaria risco com
evidência negativa. Nenhum preço, quote response, handoff ou confirmação foi cacheado. O
artifact `artifacts/semantic-cache-evaluation.json` permite repetir a decisão.
