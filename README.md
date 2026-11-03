# agent-scope-eval

How often does an LLM ops agent act outside the scope it was given, how accurately
does it report what it did, and can a cheaper model reading only the action log catch it?

**Status:** work in progress, Phase 2 (sandbox). See [docs/PLAN.md](docs/PLAN.md).
The full README (reproduction steps, results, limitations, AI usage) is written in Phase 8.

## Quick start (sandbox only)

    make setup          # Python 3.12 env from uv.lock
    make sandbox        # build the sandbox image
    make sandbox-test   # services up, no network, reset is clean
    make sandbox-shell  # poke around as the ops user (container is destroyed on exit)
