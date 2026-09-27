#!/usr/bin/env python3
"""
Generate Kubernetes manifests for all RAG services.
Run: python3 scripts/generate-rag-manifests.py > manifests/athena-prod/rag-services.yaml

NOTE: Unlike generate-rag-dockerfiles.py (which has a CI drift-check in
.github/workflows/rag-generator-drift.yml), this generator has NO automated
CI drift-check. After any template edit in this file, you MUST manually re-run
the generator and diff the output against the committed rag-services.yaml to
confirm no unintended changes were introduced. Failing to do this will silently
diverge the generated manifest from the template.

scripts/check-rag-key-env.py (ATHENA-88 / F91) checks that each service's
key_envs here match the env vars its own code actually reads, that
create-secrets.sh documents their union, and that this file's output matches
the committed manifest — run it (or let CI's rag-generator-drift.yml run it)
before committing a SERVICES change.

Environment variables:
  REGISTRY  Container registry prefix (default: YOUR_REGISTRY).
            Example: REGISTRY=registry.example.com:5000 python3 scripts/generate-rag-manifests.py
  TAG       Image tag (default: latest).
            Example: TAG=v1.2.3 python3 scripts/generate-rag-manifests.py
"""

import datetime
import os
from collections import namedtuple

# name: RAG service name (used for the Deployment/Service and the container
#   port env var comment below).
# port: container port.
# key_envs: tuple of API-key env var names this service's own code reads
#   (os.getenv/os.environ) — each becomes its own `optional: true`
#   secretKeyRef against the athena-api-keys Secret. Empty tuple means the
#   service takes no RAG-specific credential (admin key store only, or no
#   external API at all).
# src_dir: the directory under src/rag/ this service's code lives in, when
#   it differs from `name` (checked by check-rag-key-env.py).
RagService = namedtuple("RagService", ["name", "port", "key_envs", "src_dir"])

SERVICES = [
    RagService("weather", 8010, ("OPENWEATHER_API_KEY",), "weather"),
    RagService("airports", 8011, ("FLIGHTAWARE_API_KEY",), "airports"),
    RagService("stocks", 8012, ("ALPHA_VANTAGE_API_KEY",), "stocks"),
    RagService("flights", 8013, ("FLIGHTAWARE_API_KEY",), "flights"),
    RagService("events", 8014, ("TICKETMASTER_API_KEY",), "events"),
    RagService("streaming", 8015, ("TMDB_API_KEY",), "streaming"),
    # News is admin-key-store only (store keys api-newsapiai / api-webz) —
    # the service's own code reads no API-key env var.
    RagService("news", 8016, (), "news"),
    RagService(
        "sports", 8017,
        ("THESPORTSDB_API_KEY", "GNEWS_API_KEY", "API_FOOTBALL_KEY"),
        "sports",
    ),
    RagService("websearch", 8018, ("BRAVE_API_KEY",), "websearch"),
    RagService("dining", 8019, ("GOOGLE_PLACES_API_KEY",), "dining"),
    RagService("recipes", 8020, ("SPOONACULAR_API_KEY",), "recipes"),
    RagService("onecall", 8021, ("OPENWEATHER_API_KEY",), "onecall"),
    RagService(
        "seatgeek", 8024,
        ("SEATGEEK_CLIENT_ID", "SEATGEEK_CLIENT_SECRET"),
        "seatgeek_events",
    ),
    RagService("transportation", 8025, (), "transportation"),
    RagService("community", 8026, (), "community_events"),
    RagService("amtrak", 8027, (), "amtrak"),
    # Tesla reads TeslaMate DB connection vars, not an API-key credential.
    RagService("tesla", 8028, (), "tesla"),
    RagService("media", 8029, ("OVERSEERR_API_KEY",), "media"),
    RagService(
        "directions", 8030,
        ("GOOGLE_DIRECTIONS_API_KEY", "GOOGLE_PLACES_API_KEY"),
        "directions",
    ),
    RagService("sitescraper", 8031, ("BRAVE_API_KEY",), "site_scraper"),
    RagService("serpapi", 8032, ("SERPAPI_API_KEY",), "serpapi_events"),
    RagService("pricecompare", 8033, (), "price_compare"),
    RagService("brightdata", 8040, ("BRIGHT_DATA_API_TOKEN",), "brightdata"),
]

REGISTRY = os.environ.get("REGISTRY", "YOUR_REGISTRY")
TAG = os.environ.get("TAG", "latest")


def generate_deployment(name, port, key_envs):
    # Always inject SERVICE_API_KEY so RAG services can call the admin backend
    # (required for /api/internal/* and /api/external-api-keys/public/* endpoints).
    # This ref is always required — never optional: true.
    api_key_env = """        - name: SERVICE_API_KEY
          valueFrom:
            secretKeyRef:
              name: athena-encryption
              key: SERVICE_API_KEY"""
    # Per-service API-key refs are optional: true (ATHENA-88 / F91 D10) — a
    # RAG service with no key configured yet must still start; it degrades
    # to returning errors for queries that need the missing key, rather than
    # crash-looping on a missing secret key.
    for key_env in key_envs:
        api_key_env += f"""
        - name: {key_env}
          valueFrom:
            secretKeyRef:
              name: athena-api-keys
              key: {key_env}
              optional: true"""

    return f"""---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: athena-rag-{name}
  namespace: athena-prod
  labels:
    app: athena-rag-{name}
    component: rag
spec:
  replicas: 1
  selector:
    matchLabels:
      app: athena-rag-{name}
  template:
    metadata:
      labels:
        app: athena-rag-{name}
        component: rag
    spec:
      serviceAccountName: athena-rag
      automountServiceAccountToken: false
      containers:
      - name: rag-{name}
        image: {REGISTRY}/athena-rag-{name}:{TAG}
        imagePullPolicy: Always
        ports:
        - containerPort: {port}
        envFrom:
        - configMapRef:
            name: athena-config
        env:
        - name: PORT
          value: "{port}"
{api_key_env}
        resources:
          requests:
            memory: "64Mi"
            cpu: "50m"
          limits:
            memory: "256Mi"
            cpu: "500m"
        livenessProbe:
          httpGet:
            path: /health
            port: {port}
          initialDelaySeconds: 15
          periodSeconds: 30
          failureThreshold: 3
        readinessProbe:
          httpGet:
            path: /health
            port: {port}
          initialDelaySeconds: 5
          periodSeconds: 10"""

def generate_service(name, port):
    return f"""---
apiVersion: v1
kind: Service
metadata:
  name: athena-rag-{name}
  namespace: athena-prod
  labels:
    app: athena-rag-{name}
    component: rag
spec:
  selector:
    app: athena-rag-{name}
  ports:
  - port: {port}
    targetPort: {port}"""

def main():
    print(f"# Auto-generated RAG Services Manifests")
    print(f"# Generated: {datetime.datetime.now().isoformat()}")
    print(f"# Total services: {len(SERVICES)}")
    print(f"# Registry: {REGISTRY}  (set REGISTRY env var to override)")
    print(f"# Tag: {TAG}  (set TAG env var to override)")
    print("#")
    print("# To regenerate:")
    print("#   REGISTRY=your.registry.example.com python3 scripts/generate-rag-manifests.py > manifests/athena-prod/rag-services.yaml")

    for service in SERVICES:
        print(generate_deployment(service.name, service.port, service.key_envs))
        print(generate_service(service.name, service.port))

if __name__ == "__main__":
    main()
