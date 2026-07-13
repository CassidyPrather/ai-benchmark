# Testbed reconstruction

This directory documents how the pilot obtained runnable SWE-bench task
environments **in an egress-restricted sandbox where the real task images could
not be pulled**. See `../PILOT.md` for the full context and blockers.

## Why reconstruct?

The `swebench-verified@1.0` tasks expect a Docker Hub base image
(`swebench/sweb.eval.x86_64.<inst>`). Both Docker Hub and the public ghcr Epoch
mirror (`ghcr.io/epoch-research/swe-bench.eval.x86_64.<inst>`) resolve their
*manifests* through the egress proxy but serve their *blobs* from CDNs that the
policy proxy denies (`production.cloudfront.docker.com`,
`pkg-containers.githubusercontent.com` — 403 CONNECT). No SWE-bench image is
pullable here.

`mcr.microsoft.com` **self-serves blobs** and is reachable, and it mirrors the
Docker Hub `python` images at `mcr.microsoft.com/mirror/docker/library/python`.
So we rebuild an *equivalent* testbed on a `python:3.9` base and keep the task's
real `tests/`, `solution/` and `tests/config.json` unchanged.

## How the image is built

`Dockerfile` (this directory) is the reconstruction for `django__django-15098`;
the other two django tasks use the identical template with a different testbed
checkout. Build inputs, prepared on the **host** (which can reach github and
pypi via the proxy):

1. `testbed/` — `git clone`/`fetch` of `django@<base_commit>` into a
   self-contained repo, copied into the build context. (The container itself has
   no github egress for a fresh clone, so the checkout is done host-side.)
2. `ca-bundle.crt` — the egress policy-proxy CA (`/root/.ccr/ca-bundle.crt` in
   this sandbox). **Not committed** (environment-specific). It is baked into the
   image trust store so build-time `pip` and verify-time `uv run` reach pypi
   through the transparent proxy.

Build command (host network so the proxy at `127.0.0.1` is reachable at build
time):

```bash
docker build --network host \
  --build-arg HTTPS_PROXY=http://127.0.0.1:34237 \
  -t local/sweb-<inst>:latest .
```

The harbor task's `environment/Dockerfile` is then just
`FROM local/sweb-<inst>:latest`, so harbor's own build needs no egress.

## Fidelity

The only departures from the real SWE-bench image are base-image provisioning:
Python 3.9 (matching SWE-bench's django-4.1/4.2 spec, so unittest emits the old
`test (module.Class)` id format that `config.json`'s PASS_TO_PASS names use), a
copied-in Python 3.11 solely for the grading step (`uv run parser.py`,
`requires-python>=3.11`), and a `conda` stub so the real `test.sh` activation
lines are no-ops. The verifier (`test.sh`), the reference solution
(`solve.sh`), the grader (`swebench==4.0.3`) and the FAIL_TO_PASS/PASS_TO_PASS
lists are the task's own, unchanged. Empirically the reconstruction reproduces
the expected grading exactly: `nop` → all PASS_TO_PASS pass and FAIL_TO_PASS
fail; `oracle` → fully resolved (see `../results.md`).
