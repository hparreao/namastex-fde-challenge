# Campanha live `live_20260831t204700z`

Execução local contra OpenAI Responses e quote-service local estável. O banco
PostgreSQL `autoseguro_validation_live_20260831t204700z` recebeu Alembic do zero;
Redis usou DB local de validação. Os JSONs não incluem prompts, respostas brutas,
capability tokens ou PII.

Resultados: 17 tentativas HTTP, 8.934 tokens, custo estimado de USD 0,0135365 e
27,325 s de duração paga. Os dois happy paths terminaram em `completed`; cada
um apresenta uma única cotação HTTP 200 e igualdade entre preço apresentado e
payload upstream. O `AgentsSDKProvider` registrou uma falha de conexão sem
status, absorvida pelo fallback determinístico, e por isso não é o default.

`snapshot-comparison.json` compara os snapshots apenas nos casos indicados e
não permite inferência estatística. `live-budget.json` é a fonte do orçamento;
`traces/` contém somente eventos técnicos sanitizados.
