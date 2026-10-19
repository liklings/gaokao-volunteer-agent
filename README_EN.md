# Gaokao Volunteer Agent (志愿领航 Agent)

English · [中文](README.md)

![license](https://img.shields.io/badge/license-MIT-blue) ![python](https://img.shields.io/badge/python-3.12-3776ab) ![fastapi](https://img.shields.io/badge/FastAPI-009688) ![pgvector](https://img.shields.io/badge/PostgreSQL-pgvector-336791)

> A multi-agent + RAG assistant for China's *gaokao* college application planning. It understands a student's situation through conversation, produces reach / match / safety tiers with a deterministic rules engine, retrieves cited evidence from policy documents, and surfaces overlooked "information-gap" universities.

![demo](docs/assets/demo.gif)

> Demo: one sentence in → conditions extracted → candidate search → reach/match/safety tiers, plus overlooked "information-gap" schools. Jiangsu data collected by the author, for experiments only.

## Why

Choosing a college application list means juggling cross-year rank conversion, subject requirements, school tiering and many policy documents. A general-purpose LLM tends to invent admission numbers and give unsupported "admission probabilities". The idea here: **let the model understand and explain; let rules and retrieval supply the facts.**

## Features

- **Multi-agent orchestration**: an Orchestrator (LLM intent routing with keyword fallback, step planning, per-user rate limiting, full trace persistence) coordinates Profile, Retrieval and Advisor agents.
- **Guarded ReAct tool calling**: agents call tools for candidate search, comparison and policy lookup; a `finalize_recommendation` guard ensures every recommendation passes the rules engine first.
- **Deterministic rules engine**: hard-constraint filtering, reach/match/safety tiering (3:4:3 quota), cross-year rank equivalence, and discovery of 985/211/Double First-Class schools in less popular cities.
- **Policy RAG**: OCR fallback for scanned PDFs, heading-aware chunking, pgvector + BM25 fused with RRF, answers cite their source passages.
- **MCP client + skill catalog** for extensible tools and on-demand domain knowledge.
- **Layered evaluation**: slot-extraction F1, hard constraints, tier structure, historical backtest, LLM-as-Judge.

![architecture](docs/assets/architecture.svg)

## Evaluation (read with the caveats)

| Item | Result | Note |
|---|---|---|
| Policy RAG hit@1 | 76.3% → 89.5% | vector baseline → structured chunking + vector/BM25 RRF; hit@5 92.1% → 100%, MRR 0.935 |
| Backtest, safety tier | 95.3% admitted | no data leakage, 2025 as the validation year |
| Slot extraction | F1 0.999 | 200 cases |
| LLM-as-Judge | 96.9% agreement with humans | calibrated on 32 human-labelled samples |

Caveats: the RAG set has only 42 questions over 2 documents (differences within 2 questions are noise); about a third of slot cases are template-generated and unreviewed; judge calibration uses just 32 samples; the safety-tier backtest did not reach the originally targeted 99.5% (the product bar was set to 95%). See `backend/tests/eval/README.md`. The policy source documents used by the RAG eval are not distributed.

## Quick start

Requires Docker (with Compose) and a [Gemini API key](https://aistudio.google.com/apikey).

```bash
git clone https://github.com/<you>/<repo>.git && cd <repo>

printf "JWT_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')\nGEMINI_API_KEY=your_key\n" > .env
docker compose up -d --build

docker compose exec backend alembic upgrade head
docker compose exec db psql -U postgres -d gaokao_agent -f /data/jiangsu_admission_data.sql
docker compose exec db psql -U postgres -d gaokao_agent -f /data/policy_vectors.sql
docker compose exec db psql -U postgres -d gaokao_agent -f /data/post_import.sql
```

Open http://localhost:8080. API docs at http://localhost:8000/docs. For a non-Docker setup see [docs/development-notes.md](docs/development-notes.md) (Chinese).

## Data

The repo ships Jiangsu-province admission data collected and organised by the author **for experiments and research only**, plus a policy vector store (no source documents). It is **not application advice; always rely on official publications.** Only Jiangsu is covered. See [data/DATA.md](data/DATA.md).

## Limitations

Jiangsu-only data; demo stage with no real-user validation; the 3:4:3 quota is heuristic; the frontend is a simple single-page prototype.

## License

Code: [MIT](LICENSE). Data: learning, research and non-commercial use only.
