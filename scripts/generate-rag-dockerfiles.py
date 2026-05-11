#!/usr/bin/env python3
"""
Generate Dockerfiles for RAG services that are missing them.
These Dockerfiles are designed to work with the src/ directory as build context.

Usage:
  python3 scripts/generate-rag-dockerfiles.py
  python3 scripts/generate-rag-dockerfiles.py --dry-run
  python3 scripts/generate-rag-dockerfiles.py --service price_compare --force
  python3 scripts/generate-rag-dockerfiles.py --check           # exits 1 if any Dockerfile drifted
  python3 scripts/generate-rag-dockerfiles.py --check-advisory  # always exits 0; writes summary
"""

import os
import sys
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

# Services with subpackages that must be on sys.path at /app/<subpackage>.
# Format: service_name -> list of (src_subpath, dest_subpath) tuples.
# For services in this dict, the canonical /app/rag_service/ COPY is replaced
# by explicit per-subpackage entries (plan step 3a, resolution ii).
SERVICE_EXTRA_COPIES: dict = {
    "price_compare": [("providers", "providers")],
}


def generate_dockerfile(service_name: str, port: int) -> str:
    """Generate Dockerfile content for a RAG service."""
    if service_name in SERVICE_EXTRA_COPIES:
        # Explicit per-subpackage copies; skip the generic /app/rag_service/ line.
        extra_copy_lines = "\n".join(
            f"COPY rag/{service_name}/{src} /app/{dest}"
            for src, dest in SERVICE_EXTRA_COPIES[service_name]
        )
        copy_block = f"COPY rag/{service_name}/main.py /app/\n{extra_copy_lines}"
    else:
        # Standard services: main.py at /app/ + full service dir at /app/rag_service/
        copy_block = (
            f"COPY rag/{service_name}/main.py /app/\n"
            f"COPY rag/{service_name}/ /app/rag_service/"
        )

    return f"""# Auto-generated Dockerfile for {service_name} RAG service
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
{copy_block}

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
"""


def main():
    parser = argparse.ArgumentParser(description="Generate Dockerfiles for RAG services")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be created without writing files")
    parser.add_argument("--force", action="store_true", help="Overwrite existing Dockerfiles")
    parser.add_argument("--service", type=str, default=None, help="Only process this service (by directory name)")
    parser.add_argument("--check", action="store_true", help="Check for drift between generator output and live Dockerfiles; exits 1 if any differ")
    parser.add_argument("--check-advisory", action="store_true", help="Same as --check but always exits 0; writes diff summary to stdout (for advisory CI)")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    rag_dir = project_root / "src" / "rag"

    # Filter to a single service if --service is provided
    services = RAG_SERVICES
    if args.service:
        services = [(name, port) for name, port in RAG_SERVICES if name == args.service]
        if not services:
            valid = ", ".join(name for name, _ in RAG_SERVICES)
            print(f"Error: unknown service '{args.service}'. Valid names: {valid}", file=sys.stderr)
            sys.exit(1)

    # Drift-check mode
    if args.check or args.check_advisory:
        drifted = []
        for service_name, port in services:
            service_dir = rag_dir / service_name
            dockerfile_path = service_dir / "Dockerfile"
            if not service_dir.exists() or not dockerfile_path.exists():
                continue
            expected = generate_dockerfile(service_name, port)
            actual = dockerfile_path.read_text()
            if expected != actual:
                drifted.append(service_name)

        if drifted:
            print("Generator/live-Dockerfile drift detected for:")
            for name in drifted:
                print(f"  - {name}")
            print()
            print("Run `python3 scripts/generate-rag-dockerfiles.py --service <name> --force` to regenerate.")
            if args.check_advisory:
                print("(Advisory mode — exiting 0)")
                sys.exit(0)
            else:
                sys.exit(1)
        else:
            print("No drift detected — all Dockerfiles match generator output.")
            sys.exit(0)

    created = []
    skipped = []
    missing_dirs = []

    for service_name, port in services:
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
