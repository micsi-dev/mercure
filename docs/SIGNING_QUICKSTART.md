# Container Signing Quick Start Guide

**TL;DR:** Sign your algorithm containers with Cosign before deploying to Mercure.

---

## For Developers: 5-Minute Setup

### 1. Install Cosign

**macOS:**
```bash
brew install cosign
```

**Linux:**
```bash
curl -O -L "https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64"
sudo mv cosign-linux-amd64 /usr/local/bin/cosign
sudo chmod +x /usr/local/bin/cosign
```

**Verify:**
```bash
cosign version
```

---

### 2. Build and Push Your Container

```bash
cd /path/to/your/algorithm
docker build -t mycompany/algorithm:v1.0 .
docker push mycompany/algorithm:v1.0
```

---

### 3. Sign the Container

```bash
cosign sign --yes mycompany/algorithm:v1.0
```

**What happens:**
1. Browser opens → Sign in with GitHub
2. Authorize Cosign
3. Signature created and uploaded
4. ✅ Done!

**Output example:**
```
Generating ephemeral keys...
Successfully signed: index.docker.io/mycompany/algorithm:v1.0
```

---

### 4. Verify Your Signature (Optional)

```bash
cosign verify \
  --certificate-identity=your-email@company.com \
  --certificate-oidc-issuer=https://github.com/login/oauth \
  mycompany/algorithm:v1.0
```

✅ **Success:** You'll see JSON with signature details

---

### 5. Provide Info to Mercure Admin

Send to your Mercure administrator:

**Email:** `your-email@company.com` (the one you signed in with)
**Issuer:** `https://github.com/login/oauth`

---

## Common Workflows

### CI/CD with GitHub Actions

Add to `.github/workflows/build.yml`:

```yaml
name: Build, Sign, and Deploy

on:
  push:
    tags:
      - 'v*'

jobs:
  build-sign-deploy:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
      id-token: write  # Required for Cosign

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
          cosign sign --yes \
            mycompany/algorithm:${{ github.ref_name }}
```

**Then provide to admin:**
- **Identity:** `https://github.com/YOUR_ORG/YOUR_REPO/.github/workflows/build.yml@refs/heads/main`
- **Issuer:** `https://token.actions.githubusercontent.com`

---

### Re-signing After Updates

**IMPORTANT:** Every time you push a new image, you MUST re-sign!

```bash
# Update code
vim algorithm.py

# Rebuild
docker build -t mycompany/algorithm:v1.1 .
docker push mycompany/algorithm:v1.1

# RE-SIGN (critical!)
cosign sign --yes mycompany/algorithm:v1.1
```

❌ **Common mistake:** Forgetting to re-sign after pushing updated image

---

## Troubleshooting

### "Error: signing [mycompany/algorithm:v1.0]: GET https://...: UNAUTHORIZED"

**Fix:** You're not logged into the registry
```bash
docker login
# Then try signing again
```

---

### "Error: failed to get provider: getting provider: no provider found for audience"

**Fix:** Missing OIDC permissions in GitHub Actions

Add to workflow:
```yaml
permissions:
  id-token: write  # Add this!
```

---

### Browser doesn't open for GitHub auth

**Fix:** Use manual device flow
```bash
COSIGN_EXPERIMENTAL=1 cosign sign --yes mycompany/algorithm:v1.0
```

Copy the URL and auth code shown in terminal to your browser.

---

## FAQ

**Q: Do I need to manage private keys?**
A: No! Cosign uses "keyless" signing with OIDC. No keys to store or rotate.

**Q: Where are signatures stored?**
A: In the same container registry as your image, as an OCI artifact.

**Q: What if I use a private registry (ECR, GCR, ACR)?**
A: Works the same! Just sign after pushing to your private registry.

**Q: Can I sign locally built images?**
A: Yes, but they must be pushed to a registry first. Signatures attach to the registry reference.

**Q: What happens if I re-push an image with the same tag?**
A: The digest changes, invalidating the old signature. You MUST re-sign.

**Q: Should I sign `:latest` tag?**
A: Not recommended. Use version tags like `:v1.0` for clear signature tracking.

**Q: Do I need to sign every time I build locally for testing?**
A: No, only for images that will run in production Mercure with `require_signature: true`.

---

## Security Best Practices

✅ **DO:**
- Use version tags (`:v1.0`, `:v2.1.3`)
- Sign immediately after pushing
- Verify signature after signing
- Use GitHub Actions with workflow identity for CI/CD
- Enable MFA on your GitHub account

❌ **DON'T:**
- Use `:latest` tag in production
- Share OIDC credentials
- Skip signing after rebuilding
- Disable signature verification in production

---

## Getting Help

- **Cosign issues:** https://github.com/sigstore/cosign/issues
- **Mercure config:** Ask your Mercure administrator
- **This guide:** See full docs in `docs/signing.md`

---

**Last updated:** 2025-01-20
