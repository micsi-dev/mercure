#!/bin/bash
#
# Mercure macOS Setup Script
# ==========================
# This script prepares Mercure for running on macOS (including Apple Silicon)
#
# Usage: ./mac-setup.sh [--build-images] [--build-getdcmtags]
#
# Options:
#   --build-images      Build Docker images locally (required for Apple Silicon)
#   --build-getdcmtags  Rebuild getdcmtags binary for ARM64
#   --help              Show this help message
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERCURE_ROOT="$(dirname "$SCRIPT_DIR")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
echo_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
echo_error() { echo -e "${RED}[ERROR]${NC} $1"; }

BUILD_IMAGES=false
BUILD_GETDCMTAGS=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --build-images)
            BUILD_IMAGES=true
            shift
            ;;
        --build-getdcmtags)
            BUILD_GETDCMTAGS=true
            shift
            ;;
        --help)
            head -20 "$0" | tail -15
            exit 0
            ;;
        *)
            echo_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "========================================"
echo "  Mercure macOS Setup"
echo "========================================"
echo ""

# Detect architecture
ARCH=$(uname -m)
echo_info "Detected architecture: $ARCH"

if [[ "$ARCH" == "arm64" ]]; then
    echo_info "Running on Apple Silicon (ARM64)"
    IS_ARM64=true
else
    echo_info "Running on Intel (x86_64)"
    IS_ARM64=false
fi

# Check Docker
echo ""
echo_info "Checking Docker..."
if ! command -v docker &> /dev/null; then
    echo_error "Docker is not installed. Please install Docker Desktop for Mac."
    exit 1
fi

if ! docker info &> /dev/null; then
    echo_error "Docker daemon is not running. Please start Docker Desktop."
    exit 1
fi
echo_info "Docker is running."

# Create required directories
echo ""
echo_info "Creating required directories..."
DIRS=("/opt/mercure/config" "/opt/mercure/data" "/opt/mercure/db" "/opt/mercure/data/incoming"
      "/opt/mercure/data/outgoing" "/opt/mercure/data/success" "/opt/mercure/data/error"
      "/opt/mercure/data/discard" "/opt/mercure/data/processing" "/opt/mercure/data/studies"
      "/opt/mercure/data/patients" "/opt/mercure/data/jobs")

for dir in "${DIRS[@]}"; do
    if [[ ! -d "$dir" ]]; then
        echo_info "Creating $dir"
        sudo mkdir -p "$dir"
        sudo chown -R $(id -u):$(id -g) "$dir"
    fi
done

# Create symlink for app
if [[ ! -L "/opt/mercure/app" ]]; then
    echo_info "Creating symlink for app..."
    sudo ln -sf "$MERCURE_ROOT/app" /opt/mercure/app
fi

# Generate services.json with correct container names
echo ""
echo_info "Generating services.json..."
cat > /opt/mercure/config/services.json << 'EOF'
{
    "receiver": {
        "name": "Receiver",
        "docker_service": "docker-receiver-1"
    },
    "router": {
        "name": "Router",
        "docker_service": "docker-router-1"
    },
    "processor": {
        "name": "Processor",
        "docker_service": "docker-processor-1"
    },
    "dispatcher": {
        "name": "Dispatcher",
        "docker_service": "docker-dispatcher-1"
    },
    "cleaner": {
        "name": "Cleaner",
        "docker_service": "docker-cleaner-1"
    },
    "bookkeeper": {
        "name": "Bookkeeper",
        "docker_service": "docker-bookkeeper-1"
    }
}
EOF

# Generate db.env if not exists
if [[ ! -f "/opt/mercure/config/db.env" ]]; then
    echo_info "Generating db.env..."
    echo "POSTGRES_PASSWORD=mercure" > /opt/mercure/config/db.env
fi

# Generate users.json if not exists
if [[ ! -f "/opt/mercure/config/users.json" ]]; then
    echo_info "Generating users.json with default admin user..."
    cat > /opt/mercure/config/users.json << 'EOF'
{
    "admin": {
        "password": "$6$rounds=656000$BrVu/9zZ82Bw5q7U$o93ZoSjhqbVYU5EUNFJLA1fIBGogdL97ZegWXvUCzZND5yNpGKuvLxpiY/BLCIT.9X3urH8yFCUJPmzXSILwm/",
        "is_admin": "True",
        "change_password": "True"
    }
}
EOF
    echo_info "Default credentials: admin / admin"
fi

# Generate mercure.json if not exists
if [[ ! -f "/opt/mercure/config/mercure.json" ]]; then
    echo_info "Generating mercure.json..."
    cat > /opt/mercure/config/mercure.json << 'EOF'
{
    "appliance_name": "mercure",
    "port": 11112,
    "accept_compressed_images": true,
    "incoming_folder": "/opt/mercure/data/incoming",
    "studies_folder": "/opt/mercure/data/studies",
    "patients_folder": "/opt/mercure/data/patients",
    "outgoing_folder": "/opt/mercure/data/outgoing",
    "success_folder": "/opt/mercure/data/success",
    "error_folder": "/opt/mercure/data/error",
    "discard_folder": "/opt/mercure/data/discard",
    "processing_folder": "/opt/mercure/data/processing",
    "jobs_folder": "/opt/mercure/data/jobs",
    "router_scan_interval": 1,
    "dispatcher_scan_interval": 1,
    "cleaner_scan_interval": 60,
    "retention": 259200,
    "emergency_clean_percentage": 90,
    "retry_delay": 900,
    "retry_max": 5,
    "series_complete_trigger": 60,
    "study_complete_trigger": 300,
    "study_forcecomplete_trigger": 600,
    "patient_complete_trigger": 60,
    "patient_forcecomplete_trigger": 120,
    "graphite_ip": "",
    "graphite_port": 2003,
    "bookkeeper": "bookkeeper:8080",
    "offpeak_start": "22:00",
    "offpeak_end": "06:00",
    "targets": {},
    "rules": {},
    "modules": {},
    "process_runner": "docker",
    "bookkeeper_api_key": "$(openssl rand -base64 24)",
    "features": {
        "dummy_target": false
    },
    "processing_logs": {
        "discard_logs": false,
        "logs_file_store": null
    }
}
EOF
fi

# Build Docker images for ARM64 if needed
if [[ "$IS_ARM64" == true ]] || [[ "$BUILD_IMAGES" == true ]]; then
    echo ""
    echo_info "Building Docker images for $ARCH..."

    cd "$MERCURE_ROOT"

    echo_info "Building base image..."
    docker build -t mercureimaging/mercure-base:latest -f docker/base/Dockerfile .

    for service in ui bookkeeper receiver cleaner dispatcher processor router worker; do
        echo_info "Building $service image..."
        docker build -t "mercureimaging/mercure-$service:latest" -f "docker/$service/Dockerfile" .
    done

    echo_info "All images built successfully."
fi

# Build getdcmtags for ARM64 if needed
if [[ "$IS_ARM64" == true ]] || [[ "$BUILD_GETDCMTAGS" == true ]]; then
    echo ""
    echo_info "Building getdcmtags for ARM64..."

    cd "$MERCURE_ROOT/getdcmtags"

    # Build the Docker image for compiling
    docker build --build-arg UBUNTU_VERSION=22.04 -t mercure-getdcmtags-build:22.04 .

    # Run the build and extract binary
    container_id=$(docker create mercure-getdcmtags-build:22.04 sh -c "qmake && make")
    docker start -a "$container_id" || true
    docker cp "$container_id:/app/getdcmtags" "$MERCURE_ROOT/app/bin/ubuntu22.04/getdcmtags"
    docker rm "$container_id"

    # Verify the binary
    file "$MERCURE_ROOT/app/bin/ubuntu22.04/getdcmtags"
    echo_info "getdcmtags built successfully."
fi

# Pull required images
echo ""
echo_info "Pulling required images..."
docker pull redis:latest || true
docker pull postgres:14-alpine || true

echo ""
echo "========================================"
echo "  Setup Complete!"
echo "========================================"
echo ""
echo "To start Mercure on macOS, run:"
echo ""
echo "  cd $SCRIPT_DIR"
echo "  docker compose -f docker-compose.yml -f docker-compose.mac.yml up -d"
echo ""
echo "Then access the UI at: http://localhost:8000"
echo "Default login: admin / admin"
echo ""
if [[ "$IS_ARM64" == true ]]; then
    echo_warn "Apple Silicon detected. Images have been built locally."
    echo_warn "The pre-built images from Docker Hub do not support ARM64."
fi
