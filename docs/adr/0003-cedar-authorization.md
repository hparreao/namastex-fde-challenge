# ADR 0003: Cedar real em shadow mode

- Status: aceito como experimento opcional; ENFORCE limitado a `CallQuote`
- Data: 2026-08-29

## Contexto

As ações sensíveis precisavam de policy as code auditável. Cedar é um motor de autorização;
ele não detecta alucinação. A expressão “semantic firewall”, quando usada, é somente uma
analogia para um gate entre decisão e ação, sem capacidade semântica implícita.

## Decisão

Schema e policies modelam `Session` como principal/resource, ações `CallLLM`, `CallQuote`,
`PersistAudit`, `CompleteSession` e `HandoffSession`, além de context tipado. A ausência de
um `permit` produz default deny. `CallQuote` requer estado `confirmation`, dados completos,
confirmação, sanitização e destino `quote-service`.

O engine é `cedarpy 4.8.7`, binding comunitário para o engine Cedar em Rust. Ele valida as
policies contra o schema no startup e avalia as decisões reais. O projeto não é oficialmente
suportado por AWS ou pelo time Cedar, razão pela qual o padrão é shadow. `POLICY_MODE=enforce`
e `POLICY_ENFORCE_ACTIONS=CallQuote` habilitam enforcement restrito. Engine indisponível ou
deny bloqueia a cotação e produz handoff seguro sem preço.

## Consequências e evidência

A suíte executa o engine real para casos Allow/Deny de todas as ações e valida fail-closed
de `CallQuote`. O enforcement não replica as condições da policy em um falso avaliador
Python; Python apenas constrói o request tipado e aplica o resultado Cedar.
