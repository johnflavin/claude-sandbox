# Conda OCI Mirror: Feasibility Report for Air-Gapped JupyterHub

## Executive Summary

**Can `conda-oci-mirror` provide a transparent pull-through proxy cache for `conda install` / `pip install` on air-gapped Jupyter pods?**

**Short answer: No -- not on its own.** `conda-oci-mirror` is a *batch mirroring* tool, not a pull-through proxy. It copies packages from upstream conda channels into an OCI registry. It does not intercept or redirect live `conda install` requests. However, it can be a useful *component* in a broader architecture that achieves the goal. This report covers what `conda-oci-mirror` actually does, what the gaps are, and what architectures would work for your setup.

---

## 1. What `conda-oci-mirror` Actually Does

[conda-oci-mirror](https://github.com/channel-mirrors/conda-oci-mirror) is a Python CLI tool that performs three operations:

| Operation | Description |
|-----------|-------------|
| `conda-oci mirror` | Copies packages from a conda channel (e.g. conda-forge) to an OCI registry you control. Always refreshes `repodata.json`. |
| `conda-oci pull-cache` | Pulls packages from an OCI registry (possibly read-only) to a local filesystem cache. |
| `conda-oci push-cache` | Pushes locally-built packages to an OCI registry, comparing against `repodata.json` to identify new content. |

Packages are stored in the OCI registry as tagged artifacts following the naming scheme:
```
<registry>/<channel>/<subdir>/<package-name>:<version>-<build>
```
For example: `harbor.staging.example.com/conda-mirror/conda-forge/linux-64/numpy:1.24.3-py311h0b4df5a_0`

### Key Characteristics

- **Batch operation, not real-time**: You run `conda-oci mirror` on a schedule (e.g. cron) to sync packages. There is no daemon that intercepts requests.
- **Selective mirroring**: You can filter by package name, subdirectory (linux-64, noarch, etc.), and channel.
- **Uses ORAS**: Authentication is via `ORAS_USER`/`ORAS_PASS` environment variables.
- **Stores repodata.json**: The channel metadata is mirrored alongside packages, maintaining conda's package resolution capability.

### What It Does NOT Do

- Does not act as an HTTP server or proxy
- Does not respond to `conda install` or `pip install` requests
- Does not provide a conda channel endpoint that clients can point to
- Has no pull-through / cache-on-demand behavior (packages must be pre-mirrored)

---

## 2. The Gap: OCI Registry != Conda Channel

Standard conda clients (`conda`, `mamba`) expect an HTTP channel serving:
```
https://<host>/<channel>/<subdir>/repodata.json
https://<host>/<channel>/<subdir>/<package-file>.conda
```

An OCI registry (Harbor, GHCR, etc.) serves artifacts via the OCI Distribution API:
```
GET /v2/<name>/manifests/<reference>
GET /v2/<name>/blobs/<digest>
```

**These are fundamentally different protocols.** Conda clients cannot natively consume packages from an OCI registry. You need a translation layer.

### The OCI-Native Future (Not Ready Yet)

- A [Conda Enhancement Proposal (CEP #70)](https://github.com/conda/ceps/pull/70) was proposed to add native `oci://` scheme support to conda/mamba/rattler. It was **closed** in March 2025 and superseded by [CEP #115 (CEP 21)](https://github.com/conda/ceps/pull/115), which is still in **draft**.
- [Pixi](https://pixi.sh) (from prefix.dev) already supports `oci://` mirrors natively, but switching your entire JupyterHub to pixi is a non-trivial change.
- The `conda-oci-forwarder` project exists as an [early prototype](https://github.com/jaimergp/conda-oci-forwarder) HTTP bridge that translates OCI artifacts back into a standard conda channel. It is **not production-ready** (the author describes it as "a very early prototype meant for testing, playing around and getting out of emergency situations").

---

## 3. Architecture Options for Your Setup

Given your constraints:
- JupyterHub launching pods on air-gapped nodes
- Harbor running on an internet-connected staging node
- Goal: transparent `conda install` / `pip install` from Jupyter notebooks

Here are the viable approaches, ranked by practicality.

### Option A: Traditional Conda/PyPI Proxy (Recommended)

**Skip OCI entirely.** Use purpose-built conda and PyPI proxy-cache servers that speak the native protocols.

```
┌──────────────────────┐     ┌─────────────────────────┐     ┌──────────────┐
│  Jupyter Pod          │────>│  Staging Node            │────>│  Internet    │
│  (air-gapped)         │     │                          │     │              │
│                       │     │  ┌─────────────────────┐ │     │  conda-forge │
│  conda install numpy  │────>│  │ Conda proxy (Nexus/ │─│────>│  pypi.org    │
│  pip install pandas   │     │  │ Artifactory/custom) │ │     │              │
│                       │     │  └─────────────────────┘ │     └──────────────┘
│  .condarc points to   │     │  ┌─────────────────────┐ │
│  staging node         │     │  │ PyPI proxy (devpi/   │ │
│  pip.conf points to   │────>│  │ proxpi/Nexus)       │ │
│  staging node         │     │  └─────────────────────┘ │
└──────────────────────┘     └─────────────────────────┘
```

**For conda:**
- Use [Sonatype Nexus Repository](https://help.sonatype.com/en/conda-repositories.html) or a conda-specific proxy
- Configure `.condarc` on the Jupyter pods to redirect all channel requests to the staging node
- `channel_alias` and `default_channels` make this fully transparent to users

**For pip:**
- Use [devpi](https://github.com/devpi/devpi) or [proxpi](https://github.com/EpicWink/proxpi) as a pull-through PyPI cache
- Configure `pip.conf` to set `index-url` to the staging node

**Pros:** Battle-tested, truly transparent, supports pull-through caching (demand-driven), no pre-mirroring needed.
**Cons:** Requires running additional services (Nexus/devpi) on the staging node. Does not leverage Harbor.

See example configs: [`condarc-option-a.yml`](./condarc-option-a.yml), [`pip-option-a.conf`](./pip-option-a.conf)

---

### Option B: conda-oci-mirror + conda-oci-forwarder + Harbor

Use `conda-oci-mirror` to populate Harbor, and `conda-oci-forwarder` to serve it back as an HTTP channel.

```
Internet-Connected Job (cron)         Staging Node (Harbor)
┌─────────────────────────┐          ┌──────────────────────┐
│ conda-oci mirror        │          │                      │
│  --channel conda-forge  │ ──push──>│  Harbor OCI Registry  │
│  --registry harbor.stg  │          │  conda-forge/linux-64 │
│  --package numpy,pandas │          │  conda-forge/noarch   │
└─────────────────────────┘          └───────────┬──────────┘
                                                  │
                                     ┌────────────▼──────────┐
                                     │ conda-oci-forwarder   │
                                     │ (HTTP translation     │
                                     │  layer on staging)    │
                                     └────────────┬──────────┘
                                                  │
                              ┌────────────────────▼──────────┐
                              │  Jupyter Pods (air-gapped)    │
                              │  .condarc ->                  │
                              │   http://staging:8000/c-forge │
                              │  conda install numpy          │
                              └───────────────────────────────┘
```

**Pros:** Leverages Harbor. Packages stored as OCI artifacts (fits container-native infra).
**Cons:**
- `conda-oci-forwarder` is an early prototype, not production-ready
- **Not a pull-through cache** -- you must pre-mirror every package users might need
- Does not help with `pip install` at all (pip/PyPI is a separate ecosystem)
- Adds complexity: cron job + forwarder service + Harbor project

See example configs: [`mirror-job.sh`](./mirror-job.sh), [`condarc-option-b.yml`](./condarc-option-b.yml), [`docker-compose-forwarder.yml`](./docker-compose-forwarder.yml)

---

### Option C: conda-oci-mirror + Local Filesystem Channel

Use `conda-oci-mirror` to pull packages to a local filesystem, then serve that directory as a conda channel via a basic HTTP server.

```
Internet-Connected Job (cron)      Staging Node
┌──────────────────────────┐      ┌──────────────────────────┐
│ conda-oci mirror         │      │                          │
│  (to Harbor)             │─push>│ Harbor OCI Registry      │
└──────────────────────────┘      └───────────┬──────────────┘
                                              │
                              ┌───────────────▼──────────────┐
                              │ conda-oci pull-cache         │
                              │  (pull from Harbor to disk)  │
                              │                              │
                              │ cache/                       │
                              │  conda-forge/                │
                              │   linux-64/repodata.json     │
                              │   linux-64/numpy-1.24.conda  │
                              │   noarch/repodata.json       │
                              └───────────────┬──────────────┘
                                              │
                              ┌───────────────▼──────────────┐
                              │ nginx / python -m http.server│
                              │ serving cache/ directory      │
                              └───────────────┬──────────────┘
                                              │
                              ┌───────────────▼──────────────┐
                              │ Jupyter Pods (air-gapped)    │
                              │ .condarc -> http://staging/  │
                              └──────────────────────────────┘
```

**Pros:** Simple, well-understood. Conda clients work natively with HTTP-served directories.
**Cons:** Still requires pre-mirroring. Two-step sync (mirror to Harbor, then pull to disk). nginx/httpd adds another service. Does not help with pip.

See example configs: [`condarc-option-c.yml`](./condarc-option-c.yml), [`nginx-conda-channel.conf`](./nginx-conda-channel.conf), [`pull-cache-job.sh`](./pull-cache-job.sh)

---

### Option D: Harbor Proxy Cache (Container Images Only)

Harbor has a built-in [proxy cache](https://goharbor.io/docs/2.3.0/administration/configure-proxy-cache/) feature, but it is designed for **container images**, not arbitrary OCI artifacts consumed by conda. Even if you pushed conda packages as OCI artifacts to an upstream registry (e.g. ghcr.io/channel-mirrors), Harbor's proxy cache would cache the OCI blobs but would not translate them into a conda-compatible HTTP channel. You would still need a forwarder.

**Verdict:** Harbor proxy cache alone does not solve this problem.

---

## 4. Recommendation

### For the simplest, most reliable path:

**Use Option A** -- deploy a proper conda proxy (Nexus, or even a simple `conda index`-served mirror) and a PyPI proxy (devpi) on your staging node. Configure `.condarc` and `pip.conf` in the Jupyter pod base image. This gives you:

- True pull-through caching (no need to pre-mirror)
- Transparent to users (`conda install` / `pip install` just work)
- Covers both conda AND pip
- Battle-tested solutions

### If you want to leverage Harbor and OCI:

**Use Option B or C** as a proof-of-concept, with the understanding that:

- You must pre-mirror packages (no on-demand pull-through)
- `conda-oci-forwarder` is experimental
- You still need a separate solution for `pip install`
- The CEP for native OCI support in conda is still in draft

### If you want to invest in the future:

Watch [CEP 21 (PR #115)](https://github.com/conda/ceps/pull/115) and the [Pixi](https://pixi.sh) ecosystem. Once conda/mamba gain native `oci://` support, Harbor-based workflows become much more viable without needing translation layers.

---

## 5. Summary Table

| Criteria | Option A (Nexus/devpi) | Option B (OCI + Forwarder) | Option C (OCI + FS) | Option D (Harbor Proxy) |
|---|---|---|---|---|
| Transparent to users | Yes | Yes | Yes | No |
| Pull-through cache | Yes | No (pre-mirror) | No (pre-mirror) | N/A for conda |
| Covers `conda install` | Yes | Yes | Yes | No |
| Covers `pip install` | Yes | No | No | No |
| Uses Harbor | No | Yes | Yes | Yes |
| Production ready | Yes | No | Mostly | N/A |
| Complexity | Medium | High | Medium | Low but insufficient |

---

## References

- [conda-oci-mirror (GitHub)](https://github.com/channel-mirrors/conda-oci-mirror)
- [conda-oci-forwarder (GitHub)](https://github.com/jaimergp/conda-oci-forwarder)
- [CEP #70 - OCI Storage (closed)](https://github.com/conda/ceps/pull/70)
- [CEP #115 / CEP 21 - OCI Storage (draft)](https://github.com/conda/ceps/pull/115)
- [Harbor Proxy Cache Documentation](https://goharbor.io/docs/2.3.0/administration/configure-proxy-cache/)
- [Conda .condarc Configuration](https://docs.conda.io/projects/conda/en/stable/user-guide/configuration/use-condarc.html)
- [Conda Mirroring Documentation](https://docs.conda.io/projects/conda/en/stable/user-guide/configuration/mirroring.html)
- [Sonatype Nexus - Conda Repositories](https://help.sonatype.com/en/conda-repositories.html)
- [devpi - PyPI Proxy/Cache](https://github.com/devpi/devpi)
- [proxpi - PyPI Caching Proxy](https://github.com/EpicWink/proxpi)
- [Python Packaging - Index Mirrors and Caches](https://packaging.python.org/en/latest/guides/index-mirrors-and-caches/)
- [ProGet - Conda Proxy](https://docs.inedo.com/docs/proget/feeds/conda/howto-conda-proxy)
