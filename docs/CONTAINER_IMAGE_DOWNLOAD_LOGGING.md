# Container Image Download Logging

## Overview

Mercure logs all container image download events with comprehensive provenance metadata to provide a tamper-evident audit trail of exactly which container images were downloaded from which registries at what time.

This logging satisfies the security requirement: **"The system shall log container image download events with timestamp, registry endpoint, repository, tag or digest, and success or failure status."**

## Purpose

This logging addresses the **repudiation risk** by creating an immutable record that makes it technically and evidentially difficult to deny:
- Which specific container images were downloaded
- When they were downloaded (precise timestamp)
- From which registry they originated
- Whether the download succeeded or failed
- The exact image digest (cryptographic hash) for content verification

## Log Format

All container image download events are logged with the prefix `CONTAINER IMAGE DOWNLOAD:` and include structured key-value pairs:

### Successful Download

```
CONTAINER IMAGE DOWNLOAD: timestamp=2025-01-20T14:32:18.123456 | registry=registry-1.docker.io | repository=micsi/mercure-module | tag=v1.2.3 | digest=sha256:abc123def456... | full_digest=micsi/mercure-module@sha256:abc123def456... | duration=3.2s | status=SUCCESS
```

### Failed Download (Registry Unavailable)

```
CONTAINER IMAGE DOWNLOAD: timestamp=2025-01-20T14:35:42.987654 | registry=registry-1.docker.io | repository=micsi/mercure-module | tag=latest | digest=N/A | duration=30.5s | status=FAILURE | error=APIError | details=Connection timeout to registry-1.docker.io
```

### Not Found (Local/Unpublished Image)

```
CONTAINER IMAGE DOWNLOAD: timestamp=2025-01-20T14:38:01.456789 | registry=registry-1.docker.io | repository=local-dev-module | tag=latest | digest=N/A | duration=2.1s | status=NOT_FOUND | note=local_or_unpublished_image
```

## Logged Fields

| Field | Description | Example |
|-------|-------------|---------|
| `timestamp` | ISO 8601 timestamp of download event | `2025-01-20T14:32:18.123456` |
| `registry` | Registry endpoint (hostname:port) | `registry-1.docker.io` |
| `repository` | Repository path | `micsi/mercure-module` |
| `tag` | Image tag (if used) | `v1.2.3`, `latest` |
| `digest` | SHA256 digest hash of pulled image | `sha256:abc123def456...` |
| `full_digest` | Complete digest reference | `micsi/mercure-module@sha256:abc...` |
| `duration` | Pull duration in seconds | `3.2s` |
| `status` | Download outcome | `SUCCESS`, `FAILURE`, `NOT_FOUND` |
| `error` | Error type (if failed) | `APIError`, `NetworkError` |
| `details` | Error details (if failed) | Full exception message |

## Registry Endpoint Parsing

The system automatically parses Docker image references to extract the registry endpoint:

| Image Reference | Parsed Registry | Parsed Repository | Tag |
|-----------------|-----------------|-------------------|-----|
| `alpine:3.18` | `registry-1.docker.io` | `alpine` | `3.18` |
| `micsi/mercure-module:latest` | `registry-1.docker.io` | `micsi/mercure-module` | `latest` |
| `ghcr.io/org/image:v1.0` | `ghcr.io` | `org/image` | `v1.0` |
| `localhost:5000/myimage:dev` | `localhost:5000` | `myimage` | `dev` |
| `myimage@sha256:abc123...` | `registry-1.docker.io` | `myimage` | N/A (digest) |

## Implementation

Container image download logging is implemented in:
- **File**: `app/process/process_series.py`
- **Lines**: 331-409
- **Function**: Image pull block within `process_series()`

## Querying Download Logs

### Using journalctl (SystemD deployments)

```bash
# View all container image downloads
journalctl -u mercure_processor.service | grep "CONTAINER IMAGE DOWNLOAD"

# View only successful downloads
journalctl -u mercure_processor.service | grep "CONTAINER IMAGE DOWNLOAD" | grep "status=SUCCESS"

# View failed downloads
journalctl -u mercure_processor.service | grep "CONTAINER IMAGE DOWNLOAD" | grep "status=FAILURE"

# View downloads from specific registry
journalctl -u mercure_processor.service | grep "CONTAINER IMAGE DOWNLOAD" | grep "registry=ghcr.io"

# View downloads of specific repository
journalctl -u mercure_processor.service | grep "CONTAINER IMAGE DOWNLOAD" | grep "repository=micsi/mercure-module"

# View downloads within time range
journalctl -u mercure_processor.service --since "2025-01-20 14:00" --until "2025-01-20 15:00" | grep "CONTAINER IMAGE DOWNLOAD"

# Export to JSON for analysis
journalctl -u mercure_processor.service -o json | jq 'select(.MESSAGE | contains("CONTAINER IMAGE DOWNLOAD"))'
```

### Parsing Log Entries

Example Python script to parse download logs:

```python
import re
from datetime import datetime

def parse_download_log(log_line):
    """Parse a CONTAINER IMAGE DOWNLOAD log entry"""
    if "CONTAINER IMAGE DOWNLOAD:" not in log_line:
        return None

    fields = {}
    # Extract key=value pairs
    pattern = r'(\w+)=([^|]+)'
    matches = re.findall(pattern, log_line)

    for key, value in matches:
        fields[key.strip()] = value.strip()

    return fields

# Example usage
log = "CONTAINER IMAGE DOWNLOAD: timestamp=2025-01-20T14:32:18.123456 | registry=registry-1.docker.io | repository=micsi/mercure-module | tag=v1.2.3 | digest=sha256:abc123 | status=SUCCESS"
parsed = parse_download_log(log)
print(parsed)
# Output: {'timestamp': '2025-01-20T14:32:18.123456', 'registry': 'registry-1.docker.io', ...}
```

## Security Benefits

### 1. Non-Repudiation
- Cryptographic digest proves exact image content
- Timestamp proves when image was obtained
- Cannot claim "I never downloaded that image"

### 2. Supply Chain Verification
- Audit which registries are being used
- Detect unauthorized registry access
- Track image provenance for compliance

### 3. Incident Response
- Determine which images were present during incidents
- Correlate image downloads with processing tasks
- Identify when malicious images were introduced

### 4. Regulatory Compliance
- FDA 21 CFR Part 11 audit trail requirements
- ISO 62304 software provenance tracking
- IEC 62443 supply chain security

## Integration with Other Security Controls

This logging complements other container security controls:

| Control | Integration |
|---------|-------------|
| **Container Signing** (`docs/signing.md`) | Digest from download log can be verified against signature |
| **Pull Duration Logging** | Excessive delays logged with same provenance metadata |
| **Signature Verification** | Failed verification can be correlated with download event |
| **Cached Image Usage** | Distinguishes fresh downloads from cache hits |

## Example: Audit Workflow

1. **Identify images used in time period**
   ```bash
   journalctl -u mercure_processor --since "2025-01-20" | grep "CONTAINER IMAGE DOWNLOAD.*SUCCESS"
   ```

2. **Extract unique digests**
   ```bash
   journalctl -u mercure_processor | grep "CONTAINER IMAGE DOWNLOAD.*SUCCESS" | \
     grep -oP 'digest=\K[^|]+' | sort -u
   ```

3. **Verify digest matches signed image**
   ```bash
   cosign verify micsi/mercure-module@sha256:abc123... \
     --certificate-identity=user@example.com \
     --certificate-oidc-issuer=https://token.actions.githubusercontent.com
   ```

4. **Check if digest is still in local cache**
   ```bash
   docker images --digests | grep sha256:abc123...
   ```

## Retention and Archival

Container image download logs are stored in systemd journal:

- **Default retention**: 3 months or 4GB (systemd default)
- **Configuration**: `/etc/systemd/journald.conf`
- **Archival**: Use `journalctl --vacuum-time=1year` to adjust retention
- **Export**: `journalctl -u mercure_processor -o json > container_downloads.jsonl`

For long-term archival:
```bash
# Export monthly archives
journalctl -u mercure_processor --since="2025-01-01" --until="2025-02-01" \
  | grep "CONTAINER IMAGE DOWNLOAD" > archives/2025-01-downloads.log

# Compress for storage
gzip archives/2025-01-downloads.log
```

## Testing

Trigger a container image download to verify logging:

```bash
# Trigger module execution (forces image pull check)
# Via Mercure web UI: Send test DICOM to a module

# Or manually test image pull
docker pull micsi/mercure-module:latest

# Verify log entry created
journalctl -u mercure_processor --since "5 minutes ago" | grep "CONTAINER IMAGE DOWNLOAD"
```

## Troubleshooting

### No download logs appearing

**Cause**: Images are throttled (only checked once per hour)
**Solution**: Wait for next pull cycle or restart processor service

### Missing digest field

**Cause**: Local/unpublished image without registry digest
**Solution**: This is normal for locally-built images, digest will show as "unavailable"

### Registry shows as "registry-1.docker.io" for all images

**Cause**: Docker Hub is the default registry for short image names
**Solution**: This is correct behavior; use fully-qualified names (e.g., `ghcr.io/org/image`) for other registries

## References

- **Source Code**: `app/process/process_series.py:277-409`
- **Related Documentation**:
  - `docs/signing.md` - Container signature verification
  - `docs/SIGNATURE_VERIFICATION_REQUIREMENT.md` - Regulatory compliance
- **Regulatory Standards**:
  - FDA 21 CFR Part 11.10(e) - Audit trails
  - ISO 62304:2006 Section 5.1.9 - Software configuration management
  - IEC 62443-4-1 SR 3.3 - Audit trail integrity
