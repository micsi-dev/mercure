# Container Image Signing and Verification

This document describes how to sign algorithm container images using Sigstore/Cosign and configure Mercure to verify signatures before execution.

## Overview

Mercure supports cryptographic verification of algorithm container images using **Sigstore** and **Cosign**. This provides:

- **Supply chain security**: Ensure containers haven't been tampered with
- **Identity verification**: Confirm containers were built by authorized developers
- **Audit trail**: Signatures are stored in the public Rekor transparency log
- **Keyless signing**: No need to manage private keys using OIDC identity

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [For Developers: Signing Container Images](#for-developers-signing-container-images)
3. [For Administrators: Configuring Mercure](#for-administrators-configuring-mercure)
4. [Verification Examples](#verification-examples)
5. [Troubleshooting](#troubleshooting)
6. [Security Considerations](#security-considerations)

---

## Prerequisites

### For Developers (Signing)

- Docker or compatible container runtime
- Cosign CLI tool installed
- GitHub account (or other OIDC provider)
- Push access to container registry

### For Administrators (Verification)

- Cosign CLI tool installed on the Mercure processor host
- Network access to Rekor transparency log (`https://rekor.sigstore.dev`)

### Installing Cosign

**Linux (binary):**
```bash
LATEST_VERSION=$(curl https://api.github.com/repos/sigstore/cosign/releases/latest | grep tag_name | cut -d '"' -f 4 | cut -d 'v' -f 2)
curl -O -L "https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64"
sudo mv cosign-linux-amd64 /usr/local/bin/cosign
sudo chmod +x /usr/local/bin/cosign
```

**macOS (Homebrew):**
```bash
brew install cosign
```

**Verify installation:**
```bash
cosign version
```

---

## For Developers: Signing Container Images

### Step 1: Build Your Algorithm Container

```bash
cd /path/to/your/algorithm
docker build -t mycompany/algorithm:v1.0 .
docker push mycompany/algorithm:v1.0
```

### Step 2: Sign the Container Image

Cosign uses **keyless signing** with OIDC providers (GitHub, Google, Microsoft). No private keys to manage!

```bash
# Sign using GitHub identity (most common)
cosign sign --yes mycompany/algorithm:v1.0
```

**What happens:**

1. Cosign opens your browser to authenticate with GitHub
2. You sign in and authorize Cosign
3. Cosign generates an ephemeral key pair
4. Signs the container image digest
5. Uploads signature to the container registry
6. Records signature in Rekor transparency log
7. Discards private key

**Output example:**
```
Generating ephemeral keys...
Retrieving signed certificate...

        Note that there may be personally identifiable information associated with this signed artifact.
        This may include the email address associated with the account with which you authenticate.
        This information will be used for signing this artifact and will be stored in public transparency logs and cannot be removed later.

By typing 'y', you attest that you grant (or have permission to grant) and agree to have this information stored permanently in transparency logs.
Are you sure you want to continue? (y/[N]): y

Successfully verified SCT...
tlog entry created with index: 123456789
Pushing signature to: index.docker.io/mycompany/algorithm
```

### Step 3: Verify Your Signature (Optional but Recommended)

```bash
cosign verify \
  --certificate-identity=your-email@company.com \
  --certificate-oidc-issuer=https://github.com/login/oauth \
  mycompany/algorithm:v1.0
```

**If successful, you'll see:**
```json
[
  {
    "critical": {
      "identity": {
        "docker-reference": "index.docker.io/mycompany/algorithm"
      },
      "image": {
        "docker-manifest-digest": "sha256:abc123..."
      },
      "type": "cosign container image signature"
    },
    "optional": {
      "Bundle": {
        "SignedEntryTimestamp": "...",
        "Payload": {
          "body": "...",
          "integratedTime": 1234567890,
          "logIndex": 123456789,
          "logID": "..."
        }
      },
      "Issuer": "https://github.com/login/oauth",
      "Subject": "your-email@company.com"
    }
  }
]
```

### Step 4: Provide Signing Details to Mercure Administrator

Give your Mercure administrator:

1. **Certificate Identity**: Your email or GitHub URL
   - Email: `developer@mycompany.com`
   - GitHub: `https://github.com/username/repo/.github/workflows/build.yml@refs/heads/main`

2. **OIDC Issuer**:
   - GitHub: `https://github.com/login/oauth`
   - Google: `https://accounts.google.com`
   - Microsoft: `https://login.microsoftonline.com/<tenant-id>/v2.0`

---

## For Administrators: Configuring Mercure

### Step 1: Install Cosign on Processor Host

For **SystemD deployment:**
```bash
# SSH to processor host
ssh processor-host

# Install cosign
sudo curl -O -L "https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64"
sudo mv cosign-linux-amd64 /usr/local/bin/cosign
sudo chmod +x /usr/local/bin/cosign

# Verify
cosign version

# Test network access to Rekor
curl -s https://rekor.sigstore.dev/api/v1/log | jq
```

For **Docker deployment:**

Cosign needs to be available in the processor container. You have two options:

**Option A: Extend the processor image (recommended for production):**

Create `Dockerfile.processor-signed`:
```dockerfile
FROM mercureimaging/mercure-processor:latest

# Install cosign
RUN curl -O -L "https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64" && \
    mv cosign-linux-amd64 /usr/local/bin/cosign && \
    chmod +x /usr/local/bin/cosign

# Verify installation
RUN cosign version
```

Build and use:
```bash
docker build -f Dockerfile.processor-signed -t mercure-processor-signed:latest .

# Update docker-compose.yml to use the new image
# processor:
#   image: mercure-processor-signed:latest
```

**Option B: Mount cosign binary into container (development only):**
```yaml
# In docker-compose.yml
processor:
  volumes:
    - /usr/local/bin/cosign:/usr/local/bin/cosign:ro
```

### Step 2: Configure Module for Signature Verification

In the Mercure UI or via configuration file, add these fields to your module:

**Via Mercure UI:**

1. Navigate to **Configuration → Modules**
2. Edit your algorithm module
3. Add the following JSON fields:

```json
{
  "docker_tag": "mycompany/algorithm:v1.0",
  "require_signature": true,
  "signature_certificate_identity": "developer@mycompany.com",
  "signature_certificate_oidc_issuer": "https://github.com/login/oauth",
  "network_mode": "none",
  "comment": "Production SaMD algorithm - signature verification enabled"
}
```

**Via Configuration File:**

Edit `/opt/mercure/config/mercure.json` (or your config location):

```json
{
  "modules": {
    "client_algorithm_v1": {
      "docker_tag": "mycompany/algorithm:v1.0",
      "require_signature": true,
      "signature_certificate_identity": "developer@mycompany.com",
      "signature_certificate_oidc_issuer": "https://github.com/login/oauth",
      "additional_volumes": "",
      "environment": "",
      "docker_arguments": "",
      "requires_root": false,
      "requires_persistence": false,
      "network_mode": "none"
    }
  }
}
```

### Step 3: Restart Processor

**SystemD:**
```bash
sudo systemctl restart mercure_processor
sudo journalctl -u mercure_processor -f
```

**Docker:**
```bash
docker-compose restart processor
docker-compose logs -f processor
```

### Step 4: Test Signature Verification

Send a test DICOM series through the system and monitor logs:

```bash
# SystemD
sudo journalctl -u mercure_processor -f | grep -i signature

# Docker
docker-compose logs -f processor | grep -i signature
```

**Expected log output on success:**
```
INFO: Verifying signature for mycompany/algorithm:v1.0 with identity=developer@mycompany.com, issuer=https://github.com/login/oauth
INFO: ✓ Signature verification PASSED for mycompany/algorithm:v1.0
INFO: Now running container: mycompany/algorithm:v1.0
```

**Expected log output on failure:**
```
ERROR: ✗ Signature verification FAILED for mycompany/algorithm:v1.0
ERROR: Cosign stderr: Error: no matching signatures...
ERROR: Container signature verification failed for mycompany/algorithm:v1.0. Aborting processing.
```

---

## Verification Examples

### Example 1: GitHub Actions Workflow Identity

For containers built by GitHub Actions, use the workflow identity:

**Developer signs in workflow:**
```yaml
# .github/workflows/build.yml
name: Build and Sign
on:
  push:
    tags:
      - 'v*'

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
      id-token: write  # Required for OIDC signing

    steps:
      - uses: actions/checkout@v4

      - name: Login to Docker Hub
        uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          push: true
          tags: mycompany/algorithm:${{ github.ref_name }}

      - name: Install Cosign
        uses: sigstore/cosign-installer@v3

      - name: Sign container
        run: |
          cosign sign --yes mycompany/algorithm:${{ github.ref_name }}
```

**Mercure configuration:**
```json
{
  "signature_certificate_identity": "https://github.com/mycompany/algorithm/.github/workflows/build.yml@refs/heads/main",
  "signature_certificate_oidc_issuer": "https://token.actions.githubusercontent.com"
}
```

### Example 2: Multiple Developers

If multiple developers can sign, use a wildcard or specific list:

**Option A: Wildcard domain (if using corporate email):**
```json
{
  "signature_certificate_identity": "*@mycompany.com",
  "signature_certificate_oidc_issuer": "https://github.com/login/oauth"
}
```

**Option B: Specific developers (more restrictive):**

Configure multiple modules or use external policy:
```json
{
  "signature_certificate_identity": "alice@mycompany.com",
  "signature_certificate_oidc_issuer": "https://github.com/login/oauth"
}
```

For complex policies, consider using **Sigstore Policy Controller** (Kubernetes) or custom verification scripts.

### Example 3: Private Registry

For private registries (e.g., AWS ECR, Google GCR, Azure ACR):

```bash
# Sign after pushing to private registry
docker push 123456789.dkr.ecr.us-east-1.amazonaws.com/algorithm:v1.0
cosign sign --yes 123456789.dkr.ecr.us-east-1.amazonaws.com/algorithm:v1.0
```

**Mercure configuration stays the same:**
```json
{
  "docker_tag": "123456789.dkr.ecr.us-east-1.amazonaws.com/algorithm:v1.0",
  "require_signature": true,
  "signature_certificate_identity": "developer@mycompany.com",
  "signature_certificate_oidc_issuer": "https://github.com/login/oauth"
}
```

---

## Troubleshooting

### Error: "cosign not installed or not accessible"

**Symptoms:**
```
ERROR: cosign not installed or not accessible: [Errno 2] No such file or directory: 'cosign'
ERROR: Install cosign: https://docs.sigstore.dev/cosign/installation/
```

**Solution:**
1. Install cosign on the processor host (see [Installing Cosign](#installing-cosign))
2. For Docker deployments, ensure cosign is in the processor container
3. Verify: `cosign version`

### Error: "Signature verification FAILED"

**Symptoms:**
```
ERROR: ✗ Signature verification FAILED for mycompany/algorithm:v1.0
ERROR: Cosign stderr: Error: no matching signatures: crypto/rsa: verification error
```

**Common causes:**

1. **Wrong certificate identity:**
   - Check the identity used during signing: `cosign verify ... 2>&1 | grep Subject`
   - Update `signature_certificate_identity` to match exactly

2. **Wrong OIDC issuer:**
   - GitHub: `https://github.com/login/oauth`
   - Google: `https://accounts.google.com`
   - Token Actions (GitHub Actions): `https://token.actions.githubusercontent.com`

3. **Image was re-pushed without re-signing:**
   - Re-pushing an image changes its digest
   - Sign again after every push

4. **Using `:latest` tag:**
   - `:latest` tag can point to different images over time
   - Use specific version tags (e.g., `:v1.0`)

**Debug steps:**
```bash
# Check if signature exists
cosign tree mycompany/algorithm:v1.0

# Verify manually with same parameters
cosign verify \
  --certificate-identity=developer@mycompany.com \
  --certificate-oidc-issuer=https://github.com/login/oauth \
  mycompany/algorithm:v1.0
```

### Error: "Signature verification timed out"

**Symptoms:**
```
ERROR: Signature verification timed out for mycompany/algorithm:v1.0
```

**Solution:**
1. Check network connectivity to Rekor: `curl https://rekor.sigstore.dev/api/v1/log`
2. Check firewall allows outbound HTTPS to `rekor.sigstore.dev`
3. Increase timeout in `process_series.py` if on slow network (currently 60s)

### Error: "Signature verification enabled but certificate identity not configured"

**Symptoms:**
```
ERROR: Signature verification enabled for mycompany/algorithm:v1.0 but certificate identity or OIDC issuer not configured
```

**Solution:**
Ensure both fields are set in module configuration:
```json
{
  "require_signature": true,
  "signature_certificate_identity": "developer@mycompany.com",
  "signature_certificate_oidc_issuer": "https://github.com/login/oauth"
}
```

---

## Security Considerations

### Best Practices

1. **Use specific version tags, not `:latest`:**
   ```json
   "docker_tag": "mycompany/algorithm:v1.0.3"  // ✓ Good
   "docker_tag": "mycompany/algorithm:latest"  // ✗ Bad
   ```

2. **Use restrictive certificate identity:**
   ```json
   // ✓ Specific email
   "signature_certificate_identity": "alice@mycompany.com"

   // ✓ Specific GitHub workflow
   "signature_certificate_identity": "https://github.com/mycompany/algorithm/.github/workflows/release.yml@refs/heads/main"

   // ⚠ Wildcard (use with caution)
   "signature_certificate_identity": "*@mycompany.com"
   ```

3. **Monitor signature verification logs:**
   ```bash
   # Set up alerting for failed verifications
   journalctl -u mercure_processor | grep "Signature verification FAILED"
   ```

4. **Require signatures for production modules:**
   ```json
   {
     "require_signature": true  // Always for production SaMD
   }
   ```

5. **Test signature verification in staging first:**
   - Configure signature verification in staging environment
   - Verify processing succeeds before deploying to production

### Threat Model

**What signature verification protects against:**

| Threat | Protected? | Explanation |
|--------|-----------|-------------|
| **Container tampering in registry** | ✅ Yes | Modified images fail signature verification |
| **Compromised registry account** | ✅ Yes | Attacker can't create valid signatures for your identity |
| **Man-in-the-middle attacks** | ✅ Yes | Signatures verified against Rekor transparency log |
| **Rollback attacks** | ✅ Yes | Rekor provides tamper-evident log of all signatures |
| **Unsigned malicious container** | ✅ Yes | Processing fails if signature missing or invalid |

**What signature verification does NOT protect against:**

| Threat | Protected? | Mitigation |
|--------|-----------|-----------|
| **Malicious code in signed container** | ❌ No | Code review, security scanning (add to CI/CD) |
| **Compromised developer account** | ⚠️ Limited | Use workflow identity, MFA, audit logs |
| **Container escape vulnerabilities** | ❌ No | Keep Docker/kernel updated, use hardened SystemD configs |
| **Stolen OIDC credentials during signing** | ⚠️ Limited | Short-lived tokens, audit Rekor for unexpected signatures |

### Compliance Considerations

For **FDA 21 CFR Part 11** and **GDPR** compliance:

1. **Audit trail:** All signatures are recorded in public Rekor transparency log with timestamp
2. **Non-repudiation:** OIDC identity provides proof of who signed
3. **Access control:** Only authorized OIDC identities can create valid signatures
4. **Data integrity:** Cryptographic verification ensures container hasn't been modified

**Regulatory documentation:**

Include in your device design documentation:
- Copy of this signing.md document
- List of authorized signer identities
- Verification procedures
- Incident response plan for failed signature verification

---

## Advanced Topics

### Offline/Air-Gapped Environments

For air-gapped deployments without internet access to Rekor:

**Option 1: Use key-based signing (instead of keyless):**
```bash
# Generate key pair
cosign generate-key-pair

# Sign with key
cosign sign --key cosign.key mycompany/algorithm:v1.0

# Verify with public key
cosign verify --key cosign.pub mycompany/algorithm:v1.0
```

**Mercure processor needs custom verification script** (current implementation requires Rekor).

**Option 2: Run private Sigstore infrastructure:**
- Deploy private Rekor instance
- Deploy private Fulcio CA
- Configure cosign to use private endpoints
- **Complex setup** - see https://docs.sigstore.dev/cosign/private_deployment/

### Multi-Signature Requirements

Require multiple developers to sign (N-of-M signatures):

**Not currently supported by Mercure's verification logic.** Would require custom implementation.

**Workaround:** Use policy-based verification with external tool:
```bash
# In custom script called before Mercure processing
cosign verify --certificate-identity=alice@company.com ... &&
cosign verify --certificate-identity=bob@company.com ... &&
mercure-processor
```

---

## References

- **Sigstore Documentation:** https://docs.sigstore.dev/
- **Cosign GitHub:** https://github.com/sigstore/cosign
- **Rekor Transparency Log:** https://rekor.sigstore.dev/
- **Keyless Signing Blog:** https://blog.sigstore.dev/cosign-2-0-released/
- **SLSA Framework:** https://slsa.dev/ (related supply chain security)

---

## Support

For issues with:
- **Cosign itself:** https://github.com/sigstore/cosign/issues
- **Mercure signature verification:** https://github.com/mercure-imaging/mercure/issues
- **This documentation:** File issue or submit PR

**Last updated:** 2025-01-20
