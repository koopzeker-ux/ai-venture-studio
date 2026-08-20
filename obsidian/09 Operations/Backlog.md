# Backlog

## Now (M2.1)
- [x] Run Docker stack successfully
- [x] Configure Telegram bot token/chat id
- [x] Create a sample opportunity and verify alert threshold
- [ ] Hacker News connector + generic RSS/Atom connector (BUILDER)
- [ ] Source-agnostic normalize + dedupe + candidate/evidence heuristic filter (INTELLIGENCE)
- [ ] End-to-end pipeline tests (multi-source) + client resilience tests (REVIEWER)

## Later
- [ ] Add migrations (Alembic)
- [ ] Add Researcher and Critic calls with provider abstraction (needs
      an approved/budgeted model provider first)
- [ ] Add cost logging per model/tool call
- [ ] Reddit connector — only after explicit confirmation our
      commercial use is covered by Reddit's Data API Terms
- [ ] YouTube Data API connector — only after a dedicated ToS/data-
      retention compliance review (indefinite storage looks restricted)
- [ ] Google Trends connector — only if/when the official Trends API
      (currently gated alpha) opens up; no unofficial scraping
- [ ] More RSS feeds / web-search / reviews / competitor sources
- [ ] Real semantic clustering/deduplication across signals
- [ ] Automated scheduling (cron/worker) for collectors
- [ ] MCP/tool gateway
- [ ] Mission Control
- [ ] Experiment Engine
- [ ] Advanced queues/caching only if justified
