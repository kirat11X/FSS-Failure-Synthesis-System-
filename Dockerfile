# ==============================================================================
# Failure Synthesis System (FSS) — Multi-Runtime Dockerfile
#
# Provides:
#   - OpenJDK 21 (Eclipse Temurin)
#   - Python 3.11+ Virtual Environment
#   - Pre-compiled Java BlockStore & AF_UNIX Bridge
#   - Modernized 8-Part Streamlit Failure Synthesis Dashboard
# ==============================================================================

FROM eclipse-temurin:21-jdk AS base

# Prevent interactive prompts during apt operations
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8501 \
    SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    NO_PROXY="127.0.0.1,localhost" \
    no_proxy="127.0.0.1,localhost"

# Install Python 3.11, build essentials, and runtime utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    curl \
    procps \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set up isolated Python virtual environment
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Set application working directory
WORKDIR /app

# Optimize build cache: install dependencies first
COPY pyproject.toml requirements.txt requirements-app.txt ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements-app.txt

# Copy repository source code
COPY . .

# Install FSS package in editable mode with development/test dependencies
RUN pip install --no-cache-dir -e . && \
    pip install --no-cache-dir pytest hypothesis

# Pre-compile Java BlockStore engine and JEP-380 IPC Bridge
RUN chmod +x bridge/compile.sh start.sh start_all.sh run_pipeline.sh && \
    bash bridge/compile.sh

# Ensure storage directory is initialized and writable
RUN mkdir -p bridge/classes /tmp && \
    touch bridge/.perm_check && rm -f bridge/.perm_check

# Expose Streamlit dashboard port
EXPOSE 8501

# Health check probing Streamlit's native health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS --noproxy "127.0.0.1,localhost" http://127.0.0.1:${PORT:-8501}/_stcore/health || exit 1

# Default execution: run start.sh launcher
CMD ["./start.sh"]
