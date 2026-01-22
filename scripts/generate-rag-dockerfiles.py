#!/usr/bin/env python3
"""
Generate Dockerfiles for RAG services that are missing them.
These Dockerfiles are designed to work with the src/ directory as build context.

Usage:
  python3 scripts/generate-rag-dockerfiles.py
  python3 scripts/generate-rag-dockerfiles.py --dry-run
"""

import os
import argparse
from pathlib import Path

# RAG service definitions: (directory_name, port)
RAG_SERVICES = [
    ("weather", 8010),
    ("airports", 8011),
    ("stocks", 8012),
    ("flights", 8013),
    ("events", 8014),
    ("streaming", 8015),
    ("news", 8016),
    ("sports", 8017),
    ("websearch", 8018),
    ("dining", 8019),
    ("recipes", 8020),
    ("onecall", 8021),
    ("seatgeek_events", 8024),
    ("transportation", 8025),
    ("community_events", 8026),
    ("amtrak", 8027),
    ("tesla", 8028),
    ("media", 8029),
    ("directions", 8030),
    ("site_scraper", 8031),
    ("serpapi_events", 8032),
    ("price_compare", 8033),
    ("brightdata", 8040),
]

def generate_dockerfile(service_name: str, port: int) -> str:
    """Generate Dockerfile content for a RAG service."""
    return f'''# Auto-generated Dockerfile for {service_name} RAG service
# Build context should be src/ directory:
#   docker build -f rag/{service_name}/Dockerfile -t athena-rag-{service_name} .

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \\
    curl \\
    && rm -rf /var/lib/apt/lists/*

# Copy and install shared module first (for layer caching)
COPY shared /app/shared
RUN pip install --no-cache-dir -e /app/shared

# Copy and install service requirements
COPY rag/{service_name}/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \\
    pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY rag/{service_name}/main.py /app/
COPY rag/{service_name}/__init__.py /app/ 2>/dev/null || true

# Create non-root user
RUN useradd -m -u 1000 athena && chown -R athena:athena /app
USER athena

# Configure service
ENV PORT={port}
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE {port}

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \\
    CMD curl -f http://localhost:{port}/health || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{port}"]
'''

def main():
    parser = argparse.ArgumentParser(description="Generate Dockerfiles for RAG services")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be created without writing files")
    parser.add_argument("--force", action="store_true", help="Overwrite existing Dockerfiles")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    rag_dir = project_root / "src" / "rag"

    created = []
    skipped = []
    missing_dirs = []

    for service_name, port in RAG_SERVICES:
        service_dir = rag_dir / service_name
        dockerfile_path = service_dir / "Dockerfile"

        if not service_dir.exists():
            missing_dirs.append(service_name)
            continue

        if dockerfile_path.exists() and not args.force:
            skipped.append(service_name)
            continue

        content = generate_dockerfile(service_name, port)

        if args.dry_run:
            print(f"Would create: {dockerfile_path}")
            print(f"  Port: {port}")
        else:
            with open(dockerfile_path, "w") as f:
                f.write(content)
            created.append(service_name)
            print(f"Created: {dockerfile_path}")

    print()
    print("=" * 50)
    print("Summary:")
    print(f"  Created: {len(created)}")
    print(f"  Skipped (already exists): {len(skipped)}")
    print(f"  Missing directories: {len(missing_dirs)}")

    if missing_dirs:
        print()
        print("Missing service directories:")
        for name in missing_dirs:
            print(f"  - {name}")

    if skipped and not args.force:
        print()
        print("To overwrite existing Dockerfiles, use --force")

if __name__ == "__main__":
    main()
