# apps/

User-facing web applications for Project Athena. The primary services (orchestrator, gateway,
mode_service, RAG pipeline, SMS) live in `src/`; admin tooling lives in `admin/`. This
directory holds the two browser-facing apps.

## Directory structure

```
apps/
├── chat-embed/    # CORS-relay proxy for embedding Athena chat in external sites
└── jarvis-web/   # Jarvis voice + chat web interface (LiveKit-based voice frontend)
```

## apps/chat-embed/

A lightweight CORS-relay proxy that lets external websites embed an Athena chat widget
without running into browser same-origin restrictions. The proxy forwards requests to the
gateway and handles authentication headers server-side.

- **Runtime:** Python / FastAPI
- **Entry point:** `apps/chat-embed/main.py`
- **Dockerfile:** `apps/chat-embed/Dockerfile`

## apps/jarvis-web/

The primary voice and chat interface for Athena. Built on LiveKit for real-time audio
transport; connects to the orchestrator via the gateway.

- **Runtime:** Node.js (frontend build) + Python backend
- **Dockerfile:** `apps/jarvis-web/Dockerfile`
- **Port (backend proxy):** 3001

## Notes

- Both apps are live services — they have Dockerfiles and are deployed to the cluster.
- Build from the repo root with `--platform linux/amd64` when targeting the K8s cluster
  (see `scripts/build-and-push.sh`).
- Six stub directories (`gateway/`, `orchestrator/`, `rag/`, `share-service/`, `shared/`,
  `validators/`) that previously lived here were deleted after audit confirmation of zero
  importers (ATHENA-11, 2026-05-06). The real implementations of those services are in
  `src/`.
