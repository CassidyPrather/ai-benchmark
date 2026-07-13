# Reconstruction of the SWE-bench django__django-15098 testbed on a reachable base.
#
# The real task image swebench/sweb.eval.x86_64.django_1776_django-15098 (Docker Hub)
# and its ghcr Epoch mirror are UNREACHABLE here: their blob CDNs
# (production.cloudfront.docker.com, pkg-containers.githubusercontent.com) are
# policy-denied (403 CONNECT). We rebuild an equivalent /testbed from
# django@base_commit (cloned host-side, copied in) on a python:3.9 base mirrored
# via mcr.microsoft.com (which self-serves blobs and is reachable).
#
# Fidelity choices matching SWE-bench's django 4.1 spec ("python": "3.9"):
#   * Python 3.9 for the test run  -> unittest emits the old
#     "test (module.Class)" id format that config.json's PASS_TO_PASS names use
#     (Python 3.11 changed it to "test (module.Class.method)", which breaks the
#     swebench log parser's name matching).
#   * A Python 3.11 interpreter is copied in ONLY so the task's real
#     tests/test.sh grading step (`uv run parser.py`, requires-python>=3.11)
#     runs without uv downloading a standalone interpreter (that host is denied).
# The task's real tests/test.sh, tests/config.json and solution/solve.sh are UNCHANGED.

FROM mcr.microsoft.com/mirror/docker/library/python:3.11-bookworm AS py311

FROM mcr.microsoft.com/mirror/docker/library/python:3.9-bookworm

# --- Python 3.11 interpreter for the grading step only (same Debian 12 ABI) ---
COPY --from=py311 /usr/local/bin/python3.11 /usr/local/bin/python3.11
COPY --from=py311 /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=py311 /usr/local/lib/libpython3.11.so* /usr/local/lib/
COPY --from=py311 /usr/local/include/python3.11 /usr/local/include/python3.11
RUN ldconfig && python3.11 --version

# --- Trust the egress policy-proxy CA (build-time + verify-time HTTPS) ---
COPY ca-bundle.crt /usr/local/share/ca-certificates/ccr-proxy.crt
RUN cat /usr/local/share/ca-certificates/ccr-proxy.crt >> /etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    UV_SYSTEM_CERTS=1 UV_PYTHON_DOWNLOADS=never UV_PYTHON_PREFERENCE=only-system

# --- django testbed (cloned host-side at base_commit, self-contained git repo) ---
COPY testbed /testbed
WORKDIR /testbed
RUN git config --global --add safe.directory '*' && \
    git config --global user.email t@t.local && git config --global user.name testbot

# --- django (editable) + i18n test deps; uv from pypi (not astral.sh) ---
RUN pip install --no-cache-dir -e . pytz PyYAML tzdata && \
    pip install --no-cache-dir uv

# --- Stub conda so the real test.sh activation lines are harmless no-ops ---
RUN mkdir -p /opt/miniconda3/bin && \
    printf '#!/bin/bash\n:\n' > /opt/miniconda3/bin/activate && chmod +x /opt/miniconda3/bin/activate && \
    printf '#!/bin/bash\n:\n' > /usr/local/bin/conda && chmod +x /usr/local/bin/conda

RUN mkdir -p /logs
