# Improvement Roadmap

This roadmap turns the project review into a concrete execution plan with priorities, success criteria, and the reliability work already underway.

## Now: Reliability Hardening

Goal: make the current Lambda gateway safer under provider failures and long-running requests.

Success criteria:
- Streaming requests respect caller timeouts and fail over cleanly.
- Cache persistence is bounded and reliable enough for Lambda execution semantics.
- Repeated provider failures trip a local circuit breaker instead of repeatedly hammering the same dependency.
- These paths are covered by focused unit tests.

Planned work:
- [x] Add total timeout enforcement to provider streaming.
- [x] Replace fire-and-forget cache writes with bounded awaited persistence.
- [x] Add local circuit-breaker cooldown logic on provider failures.
- [x] Add tests for streaming fallback and circuit-breaker recovery.
- [x] Add FastAPI endpoint tests for `/v1/chat`, `/v1/chat/stream`, and `/health`.
- [ ] Add explicit application-level tracing spans or narrow the tracing claims in docs.
- [x] Validate the reliability hardening in AWS with live smoke and routing/rate-limit/cache/stream checks (May 27, 2026).

## Next: Production Confidence

Goal: improve correctness, debuggability, and deployment safety.

Success criteria:
- CI validates linting, types, and endpoint behaviour.
- Provider, auth, and cache code can be tested without AWS dependencies.
- Deployment configuration supports `dev`, `staging`, and `prod` without hardcoded names.

Planned work:
- Add Ruff, mypy, and stricter pytest coverage gates.
- Introduce app-factory patterns to simplify endpoint and dependency injection tests.
- Parameterize GitHub Actions deploy targets from Terraform outputs or environment configuration.
- Tighten local auth bypass behaviour so development shortcuts are explicit and safe.

## Later: Platform Maturity

Goal: evolve from a strong portfolio project into a more complete platform service.

Success criteria:
- Cost and usage data is queryable per caller.
- Routing decisions are explainable and observable.
- The system has a cleaner async story for background work and cache population.

Planned work:
- Add per-caller token and spend accounting.
- Add richer routing telemetry and evaluation datasets.
- Move non-critical async work to durable queues where appropriate.
- Revisit tracing, dashboards, and SLO-aligned alerting.
