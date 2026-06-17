# Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install git (needed for ExplorerAgent cloning)
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Copy configuration and source files
COPY pyproject.toml .
COPY README.md .
COPY cascade/ cascade/
COPY examples/ examples/

# Install the package with all optional dependencies
RUN pip install --no-cache-dir -e ".[all]"

EXPOSE 8000

CMD ["uvicorn", "cascade.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
