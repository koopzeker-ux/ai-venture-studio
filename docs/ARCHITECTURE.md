# High-level architecture

```text
Public/allowed sources
        |
        v
Market Intelligence -> Signal Store -> Opportunity Engine
                                      |
                                      v
                              Research + Critic
                                      |
                                      v
                           Score + Confidence
                                      |
                         threshold / approval gate
                                      |
                                      v
                              Telegram / Human
                                      |
                                      v
                              Experiment Engine

PostgreSQL = operational truth
Obsidian   = human-readable company brain
GitHub     = code + version history
Docker     = execution boundary
MCP/tools  = controlled external capabilities (next phase)
```

## Principles

1. Validate before building.
2. Evidence and hypotheses are separate.
3. Important claims need source provenance and confidence.
4. High-risk external actions require human approval.
5. Compute, tokens, APIs and human attention are capital.
6. Add agents only when a measurable workflow needs them.
