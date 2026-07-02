# Search relevance eval

- Embeddings provider: `fake`
- Generated: 2026-07-02 17:48:38 UTC
- User: `eval@example.com` | k=5 | queries: 15

> **FAKE embeddings - relevance numbers are meaningless. Only the mechanics and latency are trustworthy.**

| Query | Source | P@5 (capped) | P@5 (raw) | MRR | ms |
| --- | --- | ---: | ---: | ---: | ---: |
| turkish airlines flight booking confirmation | gmail | 0.00 | 0.00 | 0.00 | 17.1 |
| acme corp partnership proposal | gmail | 0.00 | 0.00 | 0.00 | 5.0 |
| q3 budget approval | gmail | 0.00 | 0.00 | 0.00 | 4.3 |
| sprint planning engineering notes | gmail | 1.00 | 0.20 | 0.33 | 4.1 |
| partnership proposal | gmail | 1.00 | 0.60 | 1.00 | 5.1 |
| aws monthly bill charges | gmail | 0.00 | 0.00 | 0.00 | 4.1 |
| team lunch thursday ramen | gmail | 0.00 | 0.00 | 0.00 | 4.2 |
| flight to new york | gcal | 0.00 | 0.00 | 0.00 | 7.0 |
| acme quarterly review | gcal | 1.00 | 0.20 | 0.50 | 4.6 |
| sync with john | gcal | 0.50 | 0.20 | 0.25 | 8.2 |
| acme proposal document | gdrive | 1.00 | 0.20 | 0.50 | 8.2 |
| out of office | gdrive | 1.00 | 0.20 | 0.20 | 4.6 |
| istanbul trip | gdrive | 1.00 | 0.20 | 0.20 | 4.5 |
| onboarding checklist | gdrive | 1.00 | 0.20 | 0.50 | 4.7 |
| q3 budget spreadsheet | gdrive | 1.00 | 0.20 | 1.00 | 4.8 |

## Aggregate

- Mean P@5 (capped): 0.567
- Mean P@5 (raw): 0.147
- Mean MRR: 0.299
- Latency p50: 4.7 ms
- Latency p95: 10.9 ms
- Latency mean: 6.0 ms
