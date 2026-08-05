FROM python:3.11-slim

# Install Clang, LLVM compiler infrastructure and Git
RUN apt-get update && apt-get install -y --no-install-recommends \
    clang \
    llvm \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy package sources and install
COPY pyproject.toml README.md /app/
COPY src /app/src/

RUN pip install --no-cache-dir .

WORKDIR /workspace

ENTRYPOINT ["keepout"]
CMD ["--help"]
