# Search relevance eval

- Embeddings provider: `openai`
- Generated: 2026-07-02 18:38:35 UTC
- User: `eval@example.com` | k=5 | queries: 15

| Query | Source | P@5 (capped) | P@5 (raw) | MRR | cold ms | warm ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| turkish airlines flight booking confirmation | gmail | 1.00 | 0.20 | 1.00 | 508.4 | 7.4 |
| acme corp partnership proposal | gmail | 1.00 | 0.60 | 1.00 | 430.7 | 8.9 |
| q3 budget approval | gmail | 1.00 | 0.20 | 1.00 | 424.9 | 7.4 |
| sprint planning engineering notes | gmail | 1.00 | 0.20 | 1.00 | 422.4 | 8.2 |
| partnership proposal | gmail | 1.00 | 0.60 | 1.00 | 411.8 | 6.7 |
| aws monthly bill charges | gmail | 1.00 | 0.20 | 1.00 | 431.1 | 11.7 |
| team lunch thursday ramen | gmail | 1.00 | 0.20 | 1.00 | 355.2 | 6.1 |
| flight to new york | gcal | 1.00 | 0.20 | 1.00 | 423.4 | 8.1 |
| acme quarterly review | gcal | 1.00 | 0.20 | 1.00 | 490.1 | 9.4 |
| sync with john | gcal | 1.00 | 0.40 | 1.00 | 417.4 | 10.7 |
| acme proposal document | gdrive | 1.00 | 0.20 | 1.00 | 372.6 | 7.5 |
| out of office | gdrive | 1.00 | 0.20 | 1.00 | 416.8 | 12.4 |
| istanbul trip | gdrive | 1.00 | 0.20 | 1.00 | 561.8 | 5.9 |
| onboarding checklist | gdrive | 1.00 | 0.20 | 1.00 | 350.5 | 4.0 |
| q3 budget spreadsheet | gdrive | 1.00 | 0.20 | 1.00 | 390.2 | 7.8 |

## Aggregate

- Mean P@5 (capped): 1.000
- Mean P@5 (raw): 0.267
- Mean MRR: 1.000
- Cold latency (embedding round trip included): p50 422.4 ms | p95 524.4 ms | mean 427.2 ms
- Warm latency (query-embedding cache hit): p50 7.8 ms | p95 11.9 ms | mean 8.1 ms
