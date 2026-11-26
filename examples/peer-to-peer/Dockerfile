FROM python:3.12-slim

RUN pip install poetry
RUN apt-get update && apt-get install -y --no-install-recommends curl
RUN rm -rf /var/lib/apt/lists/*

ENV POETRY_VIRTUALENVS_CREATE=false

COPY . /app
WORKDIR /app
RUN poetry install --with dev

EXPOSE 3999
