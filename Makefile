# Project Athena — common developer targets
#
# Targets:
#   smoke-rags          Build all 23 RAG images and verify import + pip check.
#   smoke-rags SERVICE=<name>   Same but for a single RAG image (e.g. make smoke-rags SERVICE=athena-rag-sports)
#   smoke-images        Build all 29 Python images and verify import + pip check.
#   smoke-images SERVICE=<name>  Same but for a single image (e.g. make smoke-images SERVICE=athena-chat-embed)
#   lock                Compile every requirements.in in the repo into its locked requirements.txt.
#   lock-upgrade         Same, but allows existing pins to move.
#   lock-check           Non-mutating: fails if any committed lock no longer matches a fresh compile.
#   audit-images         Build every Python image and pip-audit it (one allowlisted residual: PYSEC-2026-1325).
#   audit-images SCOPE=remediated  Only the two images this campaign remediates (athena-admin-backend, athena-jarvis-web).
#   check-build-tooling  Assert the pinned build-tooling triplet precedes every dependency install, repo-wide.
#
# NOTE for CI/gate authors: `make` collapses every non-zero recipe exit code
# to 2 — it cannot distinguish "findings exist" (e.g. drift detected) from
# "tool failed" (e.g. uv missing). Where an exit code is load-bearing, call
# the underlying script in scripts/ directly instead of through this file.
# These targets exist as developer conveniences, not as gate entry points.
#
# Prerequisites: docker with buildx support (smoke-*, audit-images); uv >= 0.10 (lock*).

.PHONY: smoke-rags smoke-images lock lock-upgrade lock-check audit-images check-build-tooling

smoke-rags:
	@if [ -n "$(SERVICE)" ]; then \
		scripts/smoke-rag-images.sh --service "$(SERVICE)"; \
	else \
		scripts/smoke-rag-images.sh; \
	fi

smoke-images:
	@if [ -n "$(SERVICE)" ]; then \
		scripts/smoke-images.sh --service "$(SERVICE)"; \
	else \
		scripts/smoke-images.sh; \
	fi

lock:
	@scripts/lock-requirements.sh

lock-upgrade:
	@scripts/lock-requirements.sh --upgrade

lock-check:
	@scripts/lock-requirements.sh --check

audit-images:
	@if [ "$(SCOPE)" = "remediated" ]; then \
		scripts/audit-images.sh --scope remediated; \
	else \
		scripts/audit-images.sh; \
	fi

check-build-tooling:
	@python3 scripts/check-build-tooling.py
