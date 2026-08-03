FROM python:3.11-slim as base

# The following is adapted from:
# https://sourcery.ai/blog/python-docker/

# Setup env
ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONFAULTHANDLER 1

FROM base AS python-deps

# Install pipenv and compilation dependencies
# Pinned: pipenv 2026.7.0 (released 2026-08-03) fails to install certifi as a
# transitive dependency under `pipenv install --deploy`, which builds a
# container that raises ModuleNotFoundError on the first `import requests`.
# It also computes the Pipfile hash differently from 2026.6.2 and earlier,
# which independently breaks --deploy's hash check against Pipfile.lock.
# 2026.6.2 was stable for ~2 months before that release; re-check this pin
# once a fixed pipenv release is out.
RUN pip install pipenv==2026.6.2
RUN apt-get update && apt-get install -y --no-install-recommends gcc

RUN mkdir -p /base
WORKDIR /base

# Install python dependencies in /.venv
COPY Pipfile .
COPY Pipfile.lock .
COPY setup.cfg .
COPY setup.py .
COPY pyproject.toml .
RUN PIPENV_VENV_IN_PROJECT=1 pipenv install --deploy

FROM base AS runtime

# Copy virtualenv from python-deps stage
COPY --from=python-deps /base/.venv /base/.venv
ENV PATH="/base/.venv/bin:$PATH"

# Create and switch to a new user
RUN useradd --create-home appuser
WORKDIR /home/appuser
USER appuser

# Install application into container
COPY . .

# Run the application
ENTRYPOINT ["python3", "-u", "main.py"]