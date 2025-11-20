# Regulatory Requirement: Container Image Signature Verification

## Requirement Statement

**"The system shall cryptographically verify the authenticity and integrity of algorithm container images before execution."**

---

## Implementation

Mercure implements container image signature verification using the **Sigstore** open-source framework with **Cosign** CLI tool integration.

### Technical Approach

**Signature Generation (Developer):**
1. Developer builds algorithm container and pushes to registry
2. Developer signs container using `cosign sign` with OIDC authentication (GitHub, Google, etc.)
3. Ephemeral key pair generated, signature created, and uploaded to registry
4. Signature metadata recorded in public **Rekor transparency log** (tamper-evident audit trail)
5. Private key immediately discarded (keyless signing)

**Signature Verification (Mercure Processor):**
1. Module configuration specifies signature requirements (`require_signature: true`)
2. Before container execution, processor calls `cosign verify` subprocess
3. Cosign verifies signature matches expected OIDC identity (certificate-based verification)
4. Cosign checks signature against Rekor transparency log
5. If verification fails, processing aborts with error logged
6. If verification succeeds, container executes normally

### Code Implementation

**Module Configuration** (`app/common/types.py:192-194`):
```python
class Module(BaseModel, Compat):
    # ... existing fields ...
    require_signature: Optional[bool] = False
    signature_certificate_identity: Optional[str] = ""  # e.g., "developer@company.com"
    signature_certificate_oidc_issuer: Optional[str] = ""  # e.g., "https://github.com/login/oauth"
```

**Verification Logic** (`app/process/process_series.py:92-165`):
```python
def verify_container_signature(docker_tag: str, module: Module) -> bool:
    """Verify container image signature using Sigstore/Cosign."""
    if not getattr(module, 'require_signature', False):
        return True  # Verification optional

    # Call cosign verify subprocess with identity verification
    result = subprocess.run([
        "cosign", "verify",
        docker_tag,
        "--certificate-identity", cert_identity,
        "--certificate-oidc-issuer", cert_oidc_issuer,
    ], capture_output=True, timeout=60)

    return result.returncode == 0
```

**Enforcement Point** (`app/process/process_series.py:302-305`):
```python
# After image pull, before container execution
if not verify_container_signature(docker_tag, module):
    logger.error(f"Container signature verification failed. Aborting processing.")
    return False
```

---

## Verification Method

**Installation Qualification (IQ):**
```bash
# Verify cosign installed on processor host
cosign version

# Verify network connectivity to Rekor transparency log
curl -s https://rekor.sigstore.dev/api/v1/log | jq '.treeSize'

# Expected: JSON response with tree size > 0
```

**Operational Qualification (OQ):**
```bash
# Test signature verification with signed test image
# 1. Build and sign test container
docker build -t test-algorithm:v1.0 .
docker push test-algorithm:v1.0
cosign sign --yes test-algorithm:v1.0

# 2. Configure Mercure module with signature verification
# 3. Send test DICOM series
# 4. Verify processing succeeds and logs show signature verification

# Expected log output:
# INFO: Verifying signature for test-algorithm:v1.0
# INFO: ✓ Signature verification PASSED
```

**Performance Qualification (PQ):**
```bash
# Monitor signature verification in production
journalctl -u mercure_processor | grep "Signature verification"

# Expected: All signatures pass, processing times acceptable (<60s for verification)
```

**Negative Testing:**
```bash
# Test 1: Unsigned container
docker push unsigned-algorithm:v1.0  # No cosign sign
# Configure module with require_signature: true
# Expected: Processing fails with "Signature verification FAILED"

# Test 2: Wrong identity
# Sign with alice@company.com, configure with bob@company.com
# Expected: Processing fails with "no matching signatures"

# Test 3: Modified container
docker push algorithm:v1.0
cosign sign --yes algorithm:v1.0
docker tag algorithm:v1.0 algorithm:v1.0  # Rebuild/modify
docker push algorithm:v1.0  # Push without re-signing
# Expected: Processing fails (digest mismatch)
```

---

## Configuration Example

**Production SaMD Algorithm Module:**
```json
{
  "module_name": "client_brain_segmentation_v2.1.0",
  "docker_tag": "mycompany/brain-segmentation:v2.1.0",
  "require_signature": true,
  "signature_certificate_identity": "ci-bot@mycompany.com",
  "signature_certificate_oidc_issuer": "https://github.com/login/oauth",
  "network_mode": "none",
  "requires_root": false,
  "requires_persistence": false,
  "comment": "FDA-cleared brain MRI segmentation algorithm - signature required per ISO 62304"
}
```

---

## Security Properties

### Cryptographic Guarantees

| Property | Mechanism | Assurance Level |
|----------|-----------|----------------|
| **Authenticity** | ECDSA P-256 signature with OIDC identity binding | High - Cryptographically proven signer identity |
| **Integrity** | SHA-256 digest signing | High - Detects any modification |
| **Non-repudiation** | Rekor transparency log with RFC 6962 Merkle tree | High - Publicly auditable, tamper-evident |
| **Freshness** | Timestamp in Rekor entry | Medium - Proves signature time, limited rollback protection |

### Threat Mitigation

| Threat | Mitigation | Residual Risk |
|--------|-----------|---------------|
| **Tampered container in registry** | Signature verification fails, processing aborted | ✅ Mitigated |
| **Malicious container uploaded by attacker** | Wrong identity, verification fails | ✅ Mitigated |
| **Compromised registry** | Signatures stored separately, verified against Rekor | ✅ Mitigated |
| **Man-in-the-middle during pull** | Docker uses HTTPS, signature verified post-pull | ✅ Mitigated |
| **Malicious code in legitimately signed container** | Code review required | ⚠️ Out of scope |
| **Compromised developer credentials** | Audit Rekor for unexpected signatures | ⚠️ Limited mitigation |

---

## Compliance Mapping

### FDA 21 CFR Part 11 (Electronic Records; Electronic Signatures)

| Requirement | Implementation | Evidence |
|-------------|----------------|----------|
| **§11.10(a) Validation** | Cosign verification proven cryptographically sound | Sigstore audit reports, NIST validation |
| **§11.10(d) Audit trail** | Rekor transparency log provides immutable record | Rekor entries with timestamp, identity |
| **§11.50 Signature manifestations** | Signature includes signer identity (email/URL) | Certificate Subject field in verification output |
| **§11.70 Signature/record linking** | Signature bound to container digest (SHA-256) | Cosign verification checks digest match |

### ISO 62304 (Medical Device Software Lifecycle)

| Requirement | Implementation | Evidence |
|-------------|----------------|----------|
| **5.1.1 Software development plan** | Signing process documented | docs/signing.md |
| **5.8.8 Software release** | Only signed containers deployed to production | Module config: `require_signature: true` |
| **8.1.1 Establish software maintenance plan** | Signature verification logs provide audit trail | Processor logs in `/var/log` |
| **9.8 Verification of software changes** | Re-signing required after any modification | Digest changes invalidate old signatures |

### IEC 62443 (Industrial Cybersecurity)

| Requirement | Implementation | Evidence |
|-------------|----------------|----------|
| **SR 3.4 Software integrity** | Cryptographic signature verification | Cosign ECDSA P-256 signatures |
| **SR 7.2 Provenance tracking** | OIDC identity in signature | Certificate identity verification |

---

## Operational Procedures

### Developer Workflow

1. **Build container:** `docker build -t algorithm:v1.0 .`
2. **Push to registry:** `docker push algorithm:v1.0`
3. **Sign container:** `cosign sign --yes algorithm:v1.0`
4. **Verify signature:** `cosign verify --certificate-identity=... algorithm:v1.0`
5. **Notify administrator:** Provide identity and OIDC issuer for Mercure config

### Administrator Workflow

1. **Install cosign:** On processor host (see docs/signing.md)
2. **Configure module:** Add `require_signature`, `signature_certificate_identity`, `signature_certificate_oidc_issuer`
3. **Test verification:** Send test DICOM series, verify logs show signature pass
4. **Monitor production:** Set up alerts for signature verification failures

### Incident Response (Failed Verification)

**Alert:** "Signature verification FAILED for algorithm:v1.0"

**Immediate Actions:**
1. **STOP:** Processing automatically aborted (safe state)
2. **Investigate:** Check processor logs for failure reason
3. **Verify:** Manually verify signature: `cosign verify ... algorithm:v1.0`
4. **Contact:** Notify algorithm developer if signature missing/invalid
5. **Document:** Record incident in quality system

**Root Cause Analysis:**
- Image re-pushed without re-signing?
- Wrong identity configured in Mercure?
- Compromised registry?
- Network issue preventing Rekor access?

---

## Limitations and Assumptions

**Limitations:**
1. Requires internet access to Rekor (https://rekor.sigstore.dev) - not suitable for air-gapped environments without private Sigstore deployment
2. Relies on OIDC provider security (GitHub, Google, etc.)
3. Does not verify algorithm code correctness, only authenticity/integrity
4. `:latest` tag not recommended (signature binds to digest, tag can move)

**Assumptions:**
1. Cosign CLI installed and accessible on processor host
2. Algorithm developers have access to OIDC provider (GitHub account, etc.)
3. Container registry supports OCI-compliant image storage
4. Network allows outbound HTTPS to rekor.sigstore.dev

**Not Addressed:**
- **Code vulnerabilities:** Separate security scanning required (e.g., Trivy, Grype)
- **Malicious signed code:** Code review and testing required
- **Supply chain attacks in dependencies:** SBOM and vulnerability scanning required

---

## Maintenance

**Update Frequency:**
- Review signer identities: Quarterly
- Update cosign version: When security updates released
- Audit Rekor entries: Monthly (verify no unexpected signatures)

**Change Control:**
- Adding new authorized signer: QA approval required
- Changing signature verification logic: Full validation testing required
- Disabling signature verification: Executive + QA approval required

---

## References

- Sigstore Documentation: https://docs.sigstore.dev/
- Cosign Security Model: https://github.com/sigstore/cosign/blob/main/SECURITY.md
- Rekor Specification: https://github.com/sigstore/rekor/blob/main/API.md
- Implementation: docs/signing.md

**Document Owner:** Security Team
**Last Reviewed:** 2025-01-20
**Next Review:** 2025-04-20
