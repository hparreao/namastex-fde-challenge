# Avaliação agregada do dataset

Execução em 28 de agosto de 2026 sobre `dataset/conversations.parquet`.

| Métrica | Resultado |
|---|---:|
| Mensagens | 26.470 |
| Conversas | 2.500 |
| Linhas de texto com PII detectável | 5.621 |
| Linhas com PII após redaction | 0 |
| Redaction nos padrões conhecidos | 100% |
| Extração de idade | 100% |
| Extração de ano do veículo | 100% |
| Mensagens de mídia | 1.789 |

Os preços históricos não foram usados. Eles são sorteados pelo gerador sintético e não
constituem ground truth para a API de cotação. As métricas de extração medem aderência aos
padrões do gerador e não devem ser apresentadas como evidência de desempenho em produção.
