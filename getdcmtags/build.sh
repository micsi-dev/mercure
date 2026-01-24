#!/bin/bash
set -euo pipefail

# Detect architecture
ARCH=$(uname -m)
echo "Detected architecture: $ARCH"

build_docker_image() {
    local UBUNTU_VERSION=$1

    echo "Building Docker image for Ubuntu $UBUNTU_VERSION ($ARCH)"

    # Build the Docker image (will use native architecture)
    docker build --build-arg UBUNTU_VERSION=$UBUNTU_VERSION -t mercure-getdcmtags-build:$UBUNTU_VERSION .

    if [ $? -ne 0 ]; then
        echo "Docker build failed for Ubuntu $UBUNTU_VERSION"
        return 1
    fi

    echo "Docker image for Ubuntu $UBUNTU_VERSION built successfully"
    echo "----------------------------------------"
}


build_qt_project() {
    local UBUNTU_VERSION=$1

    echo "Building getdcmtags for Ubuntu $UBUNTU_VERSION ($ARCH)"

    # Create container, build, and extract binary
    local container_id=$(docker create mercure-getdcmtags-build:$UBUNTU_VERSION sh -c "qmake && make")
    docker start -a "$container_id" || true

    # Create output directory if needed
    mkdir -p "../app/bin/ubuntu${UBUNTU_VERSION}"

    # Copy out the binary
    docker cp "$container_id:/app/getdcmtags" "../app/bin/ubuntu${UBUNTU_VERSION}/getdcmtags"
    docker rm "$container_id"

    # Verify the binary architecture
    echo "Built binary:"
    file "../app/bin/ubuntu${UBUNTU_VERSION}/getdcmtags"

    echo "Build for Ubuntu $UBUNTU_VERSION completed"
    echo "----------------------------------------"
}

# Main execution
echo "Starting getdcmtags build"
echo "======================================================="
echo ""

# Check for --single-version flag for faster builds
SINGLE_VERSION=""
if [[ "${1:-}" == "--single" ]]; then
    SINGLE_VERSION="${2:-22.04}"
    echo "Building only for Ubuntu $SINGLE_VERSION"
    build_docker_image $SINGLE_VERSION
    build_qt_project $SINGLE_VERSION
else
    # Build for each Ubuntu version
    for VERSION in 20.04 22.04 24.04; do
        build_docker_image $VERSION
    done

    # Run builds and extract executables for each Ubuntu version
    for VERSION in 20.04 22.04 24.04; do
        build_qt_project $VERSION
    done
fi

echo ""
echo "All builds completed"
echo ""
if [[ "$ARCH" == "arm64" || "$ARCH" == "aarch64" ]]; then
    echo "NOTE: Binaries were built for ARM64 architecture"
    echo "      These will work on Apple Silicon Macs and ARM64 Linux"
fi