#!/bin/bash
# Build and Deploy Jarvis Web to Thor Cluster
#
# This script builds the Docker image for linux/amd64 (thor cluster architecture)
# and deploys it to the Kubernetes cluster.
#
# Usage: ./build-and-deploy.sh [build|deploy|all]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Configuration - Override via environment variables
REGISTRY="${CONTAINER_REGISTRY:-localhost:5000}"
IMAGE_NAME="jarvis-web"
TAG="${IMAGE_TAG:-latest}"
FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${TAG}"
NAMESPACE="jarvis-web"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_prerequisites() {
    log_info "Checking prerequisites..."

    # Check if docker is available
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed or not in PATH"
        exit 1
    fi

    # Check if kubectl is available
    if ! command -v kubectl &> /dev/null; then
        log_error "kubectl is not installed or not in PATH"
        exit 1
    fi

    # Check kubectl context
    CURRENT_CONTEXT=$(kubectl config current-context 2>/dev/null || echo "none")
    if [[ "$CURRENT_CONTEXT" != *"thor"* ]] && [[ "$CURRENT_CONTEXT" != *"kubernetes-admin"* ]]; then
        log_warning "Current kubectl context is '$CURRENT_CONTEXT'"
        log_warning "Expected 'thor' or 'kubernetes-admin@kubernetes' context"
        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi

    log_success "Prerequisites check passed"
}

build_image() {
    log_info "Building Docker image for linux/amd64..."
    log_info "Image: ${FULL_IMAGE}"

    # Build for x86_64 (thor cluster architecture)
    docker buildx build \
        --platform linux/amd64 \
        -t "${FULL_IMAGE}" \
        --push \
        .

    log_success "Image built and pushed: ${FULL_IMAGE}"
}

deploy_kubernetes() {
    log_info "Deploying to Kubernetes..."

    # Apply all manifests
    kubectl apply -f k8s/deployment.yaml

    # Wait for deployment to be ready
    log_info "Waiting for deployment to be ready..."
    kubectl -n ${NAMESPACE} rollout status deployment/jarvis-web --timeout=120s

    # Show deployment status
    echo ""
    log_info "Deployment status:"
    kubectl -n ${NAMESPACE} get pods
    echo ""
    kubectl -n ${NAMESPACE} get svc
    echo ""
    kubectl -n ${NAMESPACE} get ingress

    log_success "Deployment complete!"
    echo ""
    log_info "Jarvis Web deployment ready"
    log_warning "Note: Configure DNS record in your DNS provider pointing to your load balancer IP"
}

restart_deployment() {
    log_info "Restarting deployment to pull latest image..."
    kubectl -n ${NAMESPACE} rollout restart deployment/jarvis-web
    kubectl -n ${NAMESPACE} rollout status deployment/jarvis-web --timeout=120s
    log_success "Deployment restarted"
}

show_logs() {
    log_info "Showing logs from jarvis-web pods..."
    kubectl -n ${NAMESPACE} logs -f -l app=jarvis-web --tail=100
}

show_status() {
    log_info "Current deployment status:"
    echo ""
    kubectl -n ${NAMESPACE} get all
    echo ""
    log_info "Ingress status:"
    kubectl -n ${NAMESPACE} get ingress
    echo ""
    log_info "Certificate status:"
    kubectl -n ${NAMESPACE} get certificate
}

show_usage() {
    echo "Jarvis Web Build and Deploy Script"
    echo ""
    echo "Usage: $0 [command]"
    echo ""
    echo "Commands:"
    echo "  build       Build and push Docker image"
    echo "  deploy      Deploy to Kubernetes"
    echo "  all         Build and deploy (default)"
    echo "  restart     Restart deployment (pull latest image)"
    echo "  logs        Show container logs"
    echo "  status      Show deployment status"
    echo "  help        Show this help message"
    echo ""
}

# Main
case "${1:-all}" in
    build)
        check_prerequisites
        build_image
        ;;
    deploy)
        check_prerequisites
        deploy_kubernetes
        ;;
    all)
        check_prerequisites
        build_image
        deploy_kubernetes
        ;;
    restart)
        restart_deployment
        ;;
    logs)
        show_logs
        ;;
    status)
        show_status
        ;;
    help|--help|-h)
        show_usage
        ;;
    *)
        log_error "Unknown command: $1"
        show_usage
        exit 1
        ;;
esac
