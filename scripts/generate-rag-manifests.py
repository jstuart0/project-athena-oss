#!/usr/bin/env python3
"""
Generate Kubernetes manifests for all RAG services.
Run: python3 scripts/generate-rag-manifests.py > manifests/athena-prod/rag-services.yaml
"""

import datetime

# Service definitions: (name, port, api_key_env or None)
SERVICES = [
    ("weather", 8010, "OPENWEATHER_API_KEY"),
    ("airports", 8011, None),
    ("stocks", 8012, "ALPHA_VANTAGE_API_KEY"),
    ("flights", 8013, "FLIGHTAWARE_API_KEY"),
    ("events", 8014, "TICKETMASTER_API_KEY"),
    ("streaming", 8015, "TMDB_API_KEY"),
    ("news", 8016, "NEWSAPI_KEY"),
    ("sports", 8017, "THESPORTSDB_API_KEY"),
    ("websearch", 8018, "BRAVE_API_KEY"),
    ("dining", 8019, "YELP_API_KEY"),
    ("recipes", 8020, "SPOONACULAR_API_KEY"),
    ("onecall", 8021, "OPENWEATHER_API_KEY"),
    ("seatgeek", 8024, "SEATGEEK_API_KEY"),
    ("transportation", 8025, None),
    ("community", 8026, None),
    ("amtrak", 8027, None),
    ("tesla", 8028, "TESLA_API_KEY"),
    ("media", 8029, None),
    ("directions", 8030, "GOOGLE_MAPS_API_KEY"),
    ("sitescraper", 8031, None),
    ("serpapi", 8032, "SERPAPI_KEY"),
    ("pricecompare", 8033, None),
    ("brightdata", 8040, "BRIGHTDATA_API_KEY"),
]

def generate_deployment(name, port, api_key):
    api_key_env = ""
    if api_key:
        api_key_env = f"""        - name: {api_key}
          valueFrom:
            secretKeyRef:
              name: athena-api-keys
              key: {api_key}"""

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
      containers:
      - name: rag-{name}
        image: 192.168.10.222:30500/athena-rag-{name}:latest
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
    print("#")
    print("# To regenerate: python3 scripts/generate-rag-manifests.py > manifests/athena-prod/rag-services.yaml")

    for name, port, api_key in SERVICES:
        print(generate_deployment(name, port, api_key))
        print(generate_service(name, port))

if __name__ == "__main__":
    main()
