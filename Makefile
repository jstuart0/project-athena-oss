# Project Athena — common developer targets
#
# Targets:
#   smoke-rags         Build all RAG images and verify import. Runs all 23 services.
#   smoke-rags SERVICE=<name>  Same but for a single image (e.g. make smoke-rags SERVICE=athena-rag-sports)
#
# Prerequisites: docker with buildx support.

.PHONY: smoke-rags

smoke-rags:
	@if [ -n "$(SERVICE)" ]; then \
		scripts/smoke-rag-images.sh --service "$(SERVICE)"; \
	else \
		scripts/smoke-rag-images.sh; \
	fi
