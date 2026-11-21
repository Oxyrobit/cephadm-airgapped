#!/usr/bin/env python3
"""
ceph-images.py — côté machine CONNECTÉE : liste les images de conteneur nécessaires
à la dernière version de Ceph (cephadm), une par ligne :

    quay.io/ceph/ceph:v21.2.0
    quay.io/prometheus/prometheus:v3.6.0
    ...

Aucune authentification : jetons anonymes des registres (Bearer) uniquement.
Python 3 stdlib uniquement. Respecte HTTP(S)_PROXY / NO_PROXY.

Usage :
    ./ceph-images.py              # images déployées par défaut au bootstrap
    ./ceph-images.py --all        # + toutes les images connues de cephadm
    ./ceph-images.py --latest     # signale (stderr) les tags plus récents disponibles
    ./ceph-images.py --version 21.2.0   # images d'une version précise (cible d'upgrade)
    ./mirror.sh registry.local:5000 $(./ceph-images.py)
"""

# ============================ CONFIGURATION ==================================
# Chaque variable peut être surchargée par une variable d'environnement du même nom.

# Endpoint API v2 à interroger pour chaque registre (à changer si proxy/miroir,
# ex. "https://artifactory.local/artifactory/api/docker/quay-remote").
# Le nom affiché reste toujours le nom canonique (quay.io/..., docker.io/...).
REGISTRY_QUAY = "https://quay.io"
REGISTRY_DOCKER = "https://registry-1.docker.io"
REGISTRY_GHCR = "https://ghcr.io"

# Dépôt Ceph (paquet cephadm = source de vérité des images par défaut)
CEPH_DOWNLOAD = "https://download.ceph.com"
CEPH_RELEASE = "auto"          # "auto" = dernière release publiée, ou ex. "umbrella", "tentacle"
CEPH_DISTRO = "trixie"         # suite Debian du dépôt
CEPH_ARCH = "amd64"

# Repli si le dépôt Ceph est injoignable
GITHUB_RAW = "https://raw.githubusercontent.com/ceph/ceph"

# Image Ceph principale
CEPH_IMAGE = "quay.io/ceph/ceph"

# Version imposée (vide = dernière). Équivaut à --version.
CEPH_VERSION = ""

HTTP_TIMEOUT = 30
# =============================================================================

import io
import json
import lzma
import os
import re
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

for _k in ("REGISTRY_QUAY", "REGISTRY_DOCKER", "REGISTRY_GHCR", "CEPH_DOWNLOAD",
           "CEPH_RELEASE", "CEPH_DISTRO", "CEPH_ARCH", "GITHUB_RAW", "CEPH_IMAGE"):
    if os.environ.get(_k):
        globals()[_k] = os.environ[_k]

REGISTRIES = {"quay.io": REGISTRY_QUAY, "docker.io": REGISTRY_DOCKER, "ghcr.io": REGISTRY_GHCR}

# Services déployés par défaut par `cephadm bootstrap` (clés de images.py)
BOOTSTRAP_KEYS = ["prometheus", "node_exporter", "alertmanager", "grafana"]

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
UA = "ceph-images/1.0"


def log(msg):
    print(msg, file=sys.stderr)


def http(url, method="GET", headers=None):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)


def get_bytes(url):
    with http(url) as r:
        return r.read()


# ----------------------------- Registre (anonyme) ----------------------------
_tokens = {}


def split_image(ref):
    """'quay.io/ceph/ceph:v21' -> ('quay.io', 'ceph/ceph', 'v21')"""
    name, _, tag = ref.rpartition(":")
    if "/" in tag or not name:
        name, tag = ref, "latest"
    reg, _, repo = name.partition("/")
    if "." not in reg and ":" not in reg:
        reg, repo = "docker.io", name
    if reg == "docker.io" and "/" not in repo:
        repo = "library/" + repo
    return reg, repo, tag


def anon_token(www_auth):
    """Jeton anonyme à partir de l'en-tête WWW-Authenticate: Bearer realm=...,service=...,scope=..."""
    params = dict(re.findall(r'(\w+)="([^"]*)"', www_auth))
    key = (params.get("realm"), params.get("service"), params.get("scope"))
    if key not in _tokens:
        q = {k: params[k] for k in ("service", "scope") if k in params}
        data = json.loads(get_bytes(params["realm"] + "?" + urllib.parse.urlencode(q)))
        _tokens[key] = data.get("token") or data.get("access_token")
    return _tokens[key]


def registry_request(reg, path, method="GET", headers=None):
    base = REGISTRIES.get(reg, "https://" + reg).rstrip("/")
    url = f"{base}/v2/{path}"
    headers = dict(headers or {})
    try:
        return http(url, method, headers)
    except urllib.error.HTTPError as e:
        if e.code != 401 or "Bearer" not in e.headers.get("WWW-Authenticate", ""):
            raise
        headers["Authorization"] = "Bearer " + anon_token(e.headers["WWW-Authenticate"])
        return http(url, method, headers)


def tag_exists(ref):
    reg, repo, tag = split_image(ref)
    try:
        with registry_request(reg, f"{repo}/manifests/{tag}", "HEAD", {"Accept": MANIFEST_ACCEPT}):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def list_tags(reg, repo):
    tags, path = [], f"{repo}/tags/list?n=1000"
    while path:
        with registry_request(reg, path) as r:
            tags += json.load(r).get("tags") or []
            link = r.headers.get("Link", "")
        m = re.search(r"<([^>]+)>", link)
        path = m.group(1).split("/v2/", 1)[-1] if m else None
    return tags


def version_key(tag):
    return [int(x) for x in re.findall(r"\d+", tag)]


def newest_like(tag, tags):
    """Tag le plus récent ayant la même « forme » (préfixe v, nb de composantes, suffixe)."""
    pat = re.sub(r"\d+", r"\\d+", re.escape(tag))
    same = [t for t in tags if re.fullmatch(pat, t)]
    return max(same, key=version_key) if same else None


# ----------------------------- Source cephadm --------------------------------
def latest_release():
    html = get_bytes(f"{CEPH_DOWNLOAD}/").decode()
    names = sorted(set(re.findall(r'href="debian-([a-z]+)/"', html)) - {"testing"})
    # les noms de release Ceph sont alphabétiques : le dernier = le plus récent
    for name in reversed(names):
        try:
            http(f"{CEPH_DOWNLOAD}/debian-{name}/dists/{CEPH_DISTRO}/Release", "HEAD").close()
            return name
        except urllib.error.HTTPError:
            continue
    raise RuntimeError(f"aucune release Ceph publiée pour {CEPH_DISTRO}")


def deb_member(deb, prefix):
    """Extrait un membre d'une archive ar (.deb)."""
    assert deb[:8] == b"!<arch>\n", "pas un .deb"
    off = 8
    while off < len(deb):
        name = deb[off:off + 16].decode().strip().rstrip("/")
        size = int(deb[off + 48:off + 58])
        if name.startswith(prefix):
            return name, deb[off + 60:off + 60 + size]
        off += 60 + size + (size & 1)
    raise RuntimeError(prefix + " absent du .deb")


def images_py_from_deb(release):
    repo = f"{CEPH_DOWNLOAD}/debian-{release}"
    pkgs = get_bytes(f"{repo}/dists/{CEPH_DISTRO}/main/binary-{CEPH_ARCH}/Packages").decode()
    block = next(b for b in pkgs.split("\n\n") if re.search(r"^Package: cephadm$", b, re.M))
    version = re.search(r"^Version: (\S+)", block, re.M).group(1)
    filename = re.search(r"^Filename: (\S+)", block, re.M).group(1)
    log(f"# cephadm {version} ({repo}/{filename})")
    name, data = deb_member(get_bytes(f"{repo}/{filename}"), "data.tar")
    if name.endswith(".xz"):
        data = lzma.decompress(data)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        zapp = tar.extractfile("./usr/sbin/cephadm").read()
    with zipfile.ZipFile(io.BytesIO(zapp)) as z:
        src = z.read("ceph/cephadm/images.py").decode()
        try:
            const = z.read("cephadmlib/constants.py").decode()
            m = re.search(r"^DEFAULT_IMAGE\s*=\s*'([^']+)'", const, re.M)
            if m and not m.group(1).startswith(CEPH_IMAGE + ":"):
                log(f"# ATTENTION : DEFAULT_IMAGE du paquet = {m.group(1)} "
                    f"-> toujours lancer 'cephadm --image {CEPH_IMAGE}:v{version.split('-')[0]} bootstrap ...'")
        except KeyError:
            pass
    return version.split("-")[0], src


def images_py_from_github(version):
    """images.py (>= 20.x) ou, pour les versions plus anciennes, mgr/cephadm/module.py."""
    for path in ("src/python-common/ceph/cephadm/images.py", "src/pybind/mgr/cephadm/module.py"):
        url = f"{GITHUB_RAW}/v{version}/{path}"
        try:
            src = get_bytes(url).decode()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        if parse_images(src):
            log(f"# source : {url}")
            return src
    raise RuntimeError(f"version v{version} introuvable sur {GITHUB_RAW} (tag inexistant ?)")


# ancien format (<= 19.x) : DEFAULT_ALERT_MANAGER_IMAGE = '...'
_LEGACY_KEYS = {"ALERT_MANAGER": "alertmanager"}


def parse_images(src):
    """{key: image_ref} depuis DefaultImages (_create_image('ref', 'key')) ou DEFAULT_*_IMAGE."""
    imgs = {k: ref for ref, k in re.findall(r"_create_image\(\s*'([^']+)'\s*,\s*'([^']+)'", src)}
    if not imgs:
        for name, ref in re.findall(r"^DEFAULT_([A-Z0-9_]+)_IMAGE\s*=\s*'([^']+)'", src, re.M):
            imgs[_LEGACY_KEYS.get(name, name.lower())] = ref
    return imgs


# ----------------------------------- main ------------------------------------
def main():
    argv, args, target = sys.argv[1:], set(), os.environ.get("CEPH_VERSION", "")
    while argv:
        a = argv.pop(0)
        if a == "--version" and argv:
            target = argv.pop(0)
        elif a.startswith("--version="):
            target = a.split("=", 1)[1]
        elif a in ("--all", "--latest", "-h", "--help"):
            args.add(a)
        else:
            args.add("--bad")
    if args & {"-h", "--help", "--bad"}:
        print(__doc__.strip(), file=sys.stderr)
        return 2 if "--bad" in args else 0
    target = target.lstrip("v")

    if target:
        # Version imposée (ex. cible d'un upgrade) : images.py au tag GitHub correspondant
        if not re.fullmatch(r"\d+\.\d+\.\d+", target):
            raise RuntimeError(f"version invalide : {target} (attendu X.Y.Z)")
        log(f"# version demandée : v{target}")
        version, src = target, images_py_from_github(target)
    else:
        # 1. dernier tag stable de l'image Ceph sur le registre
        reg, repo, _ = split_image(CEPH_IMAGE + ":x")
        stable = [t for t in list_tags(reg, repo) if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
        latest_ceph = max(stable, key=version_key)
        log(f"# dernier tag {CEPH_IMAGE} : {latest_ceph}")

        # 2. liste des images de cephadm correspondant
        release = latest_release() if CEPH_RELEASE == "auto" else CEPH_RELEASE
        try:
            version, src = images_py_from_deb(release)
        except Exception as e:  # dépôt injoignable -> GitHub
            log(f"# dépôt Ceph indisponible ({e})")
            version = latest_ceph.lstrip("v")
            src = images_py_from_github(version)
        if "v" + version != latest_ceph:
            log(f"# NOTE : cephadm du dépôt '{release}' = {version}, registre = {latest_ceph} "
                f"-> on suit cephadm (images testées avec cette version)")
    defaults = parse_images(src)
    if not defaults:
        raise RuntimeError("aucune image trouvée dans images.py")

    keys = list(defaults) if "--all" in args else BOOTSTRAP_KEYS
    images = [f"{CEPH_IMAGE}:v{version}"] + [defaults[k] for k in keys if k in defaults]

    # 3. vérification de chaque tag (+ tags plus récents si --latest)
    rc = 0
    for ref in images:
        try:
            ok = tag_exists(ref)
        except Exception as e:
            log(f"# ERREUR {ref} : {e}")
            ok, rc = False, 1
        if not ok:
            log(f"# INTROUVABLE {ref}")
            rc = 1
            continue
        print(ref, flush=True)
        if "--latest" in args:
            reg, repo, tag = split_image(ref)
            try:
                newest = newest_like(tag, list_tags(reg, repo))
                if newest and version_key(newest) > version_key(tag):
                    log(f"#   plus récent disponible : {reg}/{repo}:{newest} (non validé par cephadm)")
            except Exception as e:
                log(f"#   tags non listables ({e})")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        log(f"# ERREUR : {e}")
        sys.exit(1)
