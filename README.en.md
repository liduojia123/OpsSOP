# OpsSOP

[简体中文](README.md) | English

Self-hosted fault knowledge base and SOP management for operations teams. Record incidents, attach evidence, confirm root causes, review cases, publish SOPs, and reuse the result in later incidents.

## What it does

- **Incident records**: description, logs, environment, changes, and append-only handling notes.
- **Optional AI assistant**: classification hints and investigation steps, always returned with evidence; humans confirm the root cause.
- **Case review**: confirmed cases are reviewed before they become knowledge.
- **SOP lifecycle**: draft, review, publish, version, deprecate, and collect feedback.
- **Search**: keyword search works standalone; pgvector hybrid retrieval is optional.
- **AI chat**: read-only questions over faults, cases, SOPs, todos, and monitored servers, with streamed answers and clickable citations.
- **Monitoring**: Prometheus panels linked to services; alerts can create incidents after deduplication.
- **Operations**: backups and restore, audit log, notifications, role-based access, isolated drill scripts.

AI never resolves incidents, executes commands, or publishes SOPs on its own.

## Quick start

Requires Docker Engine and Docker Compose 2.24.4+.

```bash
git clone https://github.com/liduojia123/OpsSOP.git
cd OpsSOP
git checkout v3.7.2
cp .env.example .env        # set POSTGRES_PASSWORD, SECRET_KEY, OPSSOP_ADMIN_PASSWORD
docker compose up -d --build
```

Open <http://localhost:8080>. The first start runs migrations and creates the admin account from `.env`.

## Configuration

Secrets live only in `.env` (ignored by Git). AI and embedding are optional and configured independently:

| Mode | Behavior |
|---|---|
| `disabled` (default) | No model calls; keyword search and the full business flow work |
| `cloud` | OpenAI-compatible cloud endpoint |
| `local` | Local or intranet endpoint |
| `demo` | Explicitly simulated responses, never selected automatically |

`LOCAL_ONLY=1` blocks chat and embedding from using cloud configuration; failures never fall back to cloud. Cloud mode sends sanitized prompts and knowledge excerpts to the configured service.

## Documentation

Docs are written in Chinese:

- [Product guide](docs/product-guide.md) · [Deployment](docs/deployment.md) · [Production checklist](docs/production-checklist.md)
- [Architecture](docs/architecture.md) · [Design decisions](docs/design-decisions.md) · [AI configuration](docs/ai-configuration.md) · [Search](docs/search.md) · [Chat](docs/chat.md)
- [Backup and restore](docs/backup-restore.md) · [Operations](docs/operations.md) · [Evaluation](docs/evaluation.md)
- [Changelog](CHANGELOG.md)

Local setup, tests, and code layout: [DEVELOPING.md](DEVELOPING.md) (Chinese). Contribution rules: [CONTRIBUTING.md](CONTRIBUTING.md).

## Tech stack

Vue 3, TypeScript, Vite, Element Plus, Tailwind CSS · FastAPI, SQLAlchemy, Alembic · PostgreSQL 16 + pgvector · nginx · Docker Compose.

```text
backend/app/       API, business logic, model adapters, search, evaluation
backend/alembic/   database migrations
frontend/src/      views, components, styles, API client
backend/tests/     backend test suite
frontend/tests/    component tests
docs/              product, deployment, and operations docs
```

Target scale is a single organization with 6–20 users and one application instance. No multi-tenancy, billing, or managed SLA.

## License

MIT. Third-party notices in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Report security issues per [SECURITY.md](SECURITY.md).