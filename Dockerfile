FROM python:3.12-slim

# Set to false to skip installing the Claude Code CLI (only needed for provider: claude_code).
ARG INSTALL_CLAUDE_CODE=true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JOBSEARCHER_CONFIG=/config/config.yaml \
    JOBSEARCHER_DATA_DIR=/data \
    JOBSEARCHER_CV=/cvs/master.md \
    PATH=/home/app/.local/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY jobsearcher ./jobsearcher
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app /data
USER app

# Native installer; puts `claude` in ~/.local/bin.
RUN if [ "$INSTALL_CLAUDE_CODE" = "true" ]; then \
        curl -fsSL https://claude.ai/install.sh | bash && claude --version; \
    fi

ENTRYPOINT ["jobsearcher"]
CMD ["daemon"]
