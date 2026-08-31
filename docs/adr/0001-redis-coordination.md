# ADR 0001: Redis para coordenação distribuída

- Status: aceito como recurso opcional; integração Redis real executada localmente
- Data: 2026-08-29

## Contexto

O fluxo anterior usava rate limiting em memória e não serializava mensagens da mesma
sessão. Duas confirmações concorrentes podiam atravessar transações independentes e
executar duas cotações.

## Decisão

Redis concentra rate limiting compartilhado, respostas por `Idempotency-Key`, lock curto
por sessão e estado do circuit breaker. Chaves externas são transformadas em SHA-256 antes
de chegar ao Redis. O payload de idempotência contém somente a resposta já sanitizada.

Rate limiting degrada explicitamente para memória local quando Redis falha. Idempotência e
lock falham fechados com HTTP 503 porque continuar reabre a corrida. O circuit breaker falha
aberto, preservando os timeouts e retries que já limitam o upstream.

## Consequências e evidência

Testes determinísticos provam uma única cotação e resposta idêntica para duas requisições
concorrentes com a mesma chave. A suíte também cobre indisponibilidade e ausência de PII no
payload coordenado. O teste contra Redis real foi executado em instância efêmera dedicada.
A campanha live também usou prefixo exclusivo por `RUN_ID` para rate limiting,
idempotência, lock e circuit breaker.

O lock tem TTL maior que o pior tempo configurado de retries, mas não entrega exactly-once
diante de crash após o quote-service aceitar a chamada. Garantia forte exigiria suporte a
idempotency key no próprio `POST /quote` ou protocolo transacional, ausente no desafio.
