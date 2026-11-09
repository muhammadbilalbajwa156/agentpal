FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
# Build context is the project directory (repo-root/agentpal), not the spec directory.
# Dependencies come only from pinned wheels downloaded beforehand (build_wheels.sh) and are
# checked against their SHA-256 list; the image build itself needs no network access.
COPY agentpal/agents/shopper/astropods/wheels /tmp/wheels
COPY agentpal/agents/shopper/astropods/wheels.sha256 agentpal/agents/shopper/astropods/requirements-image.txt /tmp/
RUN cd /tmp/wheels && sha256sum -c /tmp/wheels.sha256 \
 && pip install --no-cache-dir --no-index --find-links /tmp/wheels -r /tmp/requirements-image.txt \
 && rm -rf /tmp/wheels
# Only the demonstration package enters the image: no config, model, DB or credentials.
COPY agentpal/agents /app/agentpal/agents
RUN rm -rf /app/agentpal/agents/shopper/astropods/wheels
ENV PYTHONUNBUFFERED=1
EXPOSE 80
CMD ["uvicorn", "agentpal.agents.shopper.astropods.web:app", "--host", "0.0.0.0", "--port", "80", "--no-access-log"]
