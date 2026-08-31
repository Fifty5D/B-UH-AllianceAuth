ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential default-libmysqlclient-dev pkg-config curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY platform/requirements/ /workspace/platform/requirements/
RUN python -m pip install \
      --require-hashes \
      -r /workspace/platform/requirements/build.lock \
    && python -m pip install \
      --require-hashes \
      --no-build-isolation \
      --only-binary=:all: \
      --no-binary=celery-once \
      --no-binary=django-bootstrap-form \
      --no-binary=mysqlclient \
      -r /workspace/platform/requirements/production.lock

# These two upstream wheels are kept immutable in the last verified release.
COPY releases/moon-tax/v0.3.3/aa_structures-4.0.3-py3-none-any.whl /tmp/wheels/
COPY releases/moon-tax/v0.3.3/aa_moonmining-3.1.0.post1-py3-none-any.whl /tmp/wheels/
RUN printf '%s  %s\n' \
      'ba8493ef10a450198fa5ba3d8995e1ea661b74dca20d770beaa91bf005f558e3' \
      '/tmp/wheels/aa_structures-4.0.3-py3-none-any.whl' \
      'aa4ddbc7da64151cd6d988d971d59462360e35a5111d47a3bf56d577f454bc13' \
      '/tmp/wheels/aa_moonmining-3.1.0.post1-py3-none-any.whl' \
      | sha256sum --check --strict - \
    && python -m pip install --no-index --no-deps /tmp/wheels/*.whl

COPY apps/ /workspace/apps/
RUN python -m pip install \
      --no-build-isolation \
      --no-deps \
      -e /workspace/apps/structure-ops \
      -e /workspace/apps/mining-analytics \
      -e /workspace/apps/moon-tax

COPY platform/testauth/ /workspace/platform/testauth/
COPY tests/ /workspace/tests/

ENV PYTHONPATH=/workspace/platform/testauth \
    BUH_TEST_SETTINGS=testauth.settings.integration
WORKDIR /workspace/platform/testauth
