# cephadm-airgapped

Déployer Ceph avec **cephadm** sur une machine **sans accès Internet** (air-gap) : la liste
des images de conteneur à fournir, l'outil pour les copier dans un registre local et la
configuration podman qui va avec.

[![update-images](../../actions/workflows/update-images.yml/badge.svg)](../../actions/workflows/update-images.yml)

## Listes d'images (à jour automatiquement)

Une CI quotidienne régénère ces fichiers à partir de la dernière release Ceph :

| Fichier | Contenu |
|---|---|
| [`images.txt`](images.txt) | images déployées par un `cephadm bootstrap` standard (ceph, prometheus, node-exporter, alertmanager, grafana) |
| [`images-all.txt`](images-all.txt) | toutes les images connues de cephadm (ingress, nvmeof, loki, samba, jaeger…) |

Format : une image par ligne, par exemple `quay.io/ceph/ceph:v21.2.0`.

## Principe

```
 Machine connectée                         Réseau air-gap
 ─────────────────                         ──────────────
 ceph-images.py ─► images.txt
 mirror.sh ──── skopeo copy ──────────►  registre local (registry.local:5000)
                                                │
                                         nœud Ceph : podman + registries.conf
                                         (quay.io, docker.io… → registre local)
```

Seuls les dépôts de paquets restent nécessaires côté air-gap (miroir APT de Debian et de
`download.ceph.com/debian-<release>`). Les images, elles, passent par le registre local.

## Utilisation

### 1. Côté machine connectée

```bash
./ceph-images.py                 # images du bootstrap, une par ligne
./ceph-images.py --all           # toutes les images cephadm
./ceph-images.py --latest        # signale (stderr) les tags plus récents, non validés par cephadm
./ceph-images.py --version 21.2.0  # images d'une version précise (cible d'upgrade)

./mirror.sh registry.local:5000 $(cat images.txt)
```

- Python 3, bibliothèque standard uniquement, **sans authentification** (jetons anonymes des registres).
- Les infos vont sur stderr, la liste seule sur stdout.
- Si vous passez par un proxy ou un miroir (Artifactory, Harbor, Nexus), modifiez les variables
  en tête du script ou passez-les en variables d'environnement :

  ```bash
  REGISTRY_QUAY=https://artifactory.local/api/docker/quay-remote \
  REGISTRY_DOCKER=https://artifactory.local/api/docker/dockerhub-remote \
  HTTPS_PROXY=http://proxy:3128 ./ceph-images.py
  ```

  | Variable | Défaut |
  |---|---|
  | `REGISTRY_QUAY` | `https://quay.io` |
  | `REGISTRY_DOCKER` | `https://registry-1.docker.io` |
  | `REGISTRY_GHCR` | `https://ghcr.io` |
  | `CEPH_DOWNLOAD` | `https://download.ceph.com` |
  | `CEPH_RELEASE` | `auto` (dernière release ; ou `umbrella`, `tentacle`…) |
  | `CEPH_DISTRO` | `trixie` |
  | `CEPH_VERSION` | vide (= dernière) ; équivaut à `--version` |
  | `GITHUB_RAW` | `https://raw.githubusercontent.com/ceph/ceph` (repli) |

### 2. Côté nœud air-gap

```bash
# podman : rediriger les registres publics vers le registre local
cp registries.conf /etc/containers/registries.conf.d/99-airgap.conf
sed -i 's/registry.local:5000/<votre-registre>/' /etc/containers/registries.conf.d/99-airgap.conf

apt install cephadm ceph-common podman lvm2 chrony openssh-server

# toujours préciser l'image (voir « Bon à savoir »)
cephadm --image quay.io/ceph/ceph:v21.2.0 bootstrap --mon-ip <IP>
```

Le bootstrap affiche à la fin l'URL du dashboard, l'utilisateur et le mot de passe.

## Comment les versions sont déterminées

1. Le dernier tag stable `vX.Y.Z` de `quay.io/ceph/ceph` est lu sur le registre.
2. Le paquet `cephadm` de la dernière release est téléchargé depuis `download.ceph.com/debian-<release>`.
   Le fichier `ceph/cephadm/images.py` qu'il contient fixe les versions des images de monitoring.
   Si le dépôt ne répond pas, le même fichier est lu sur GitHub, au tag correspondant.
3. Chaque `image:tag` est vérifié sur son registre. Un tag introuvable donne un code retour ≠ 0.

## Mettre à jour Ceph

En air-gap, **la découverte des versions ne fonctionne pas, mais l'upgrade oui** :

- `ceph orch upgrade ls` et la page *Upgrade* du dashboard interrogent le registre **directement
  depuis le mgr, en HTTPS** (`https://quay.io/v2/ceph/ceph/tags/list`). Ces appels ne passent pas par
  `registries.conf` et échouent :
  `Cannot get tags from url 'https://quay.io/v2/ceph/ceph/tags/list' ... Name or service not known`.
  C'est normal : la liste des versions vient de la machine connectée (`ceph-images.py`).
- `ceph orch upgrade check|start` tirent l'image **via podman**, donc via le registre local.

Le nouveau mgr redéploie aussi le monitoring avec **ses propres** versions d'images. Il faut donc
copier les images de la **version cible**, pas seulement l'image `ceph`.

```bash
# 1. Machine connectée : images de la version cible -> registre local
./ceph-images.py --version 21.2.1 > upgrade.txt     # ou sans --version pour la dernière
./mirror.sh registry.local:5000 $(cat upgrade.txt)

# 2. Nœuds air-gap (recommandé) : outils clients à la même version, depuis le miroir APT
apt install --only-upgrade cephadm ceph-common

# 3. Vérifier : tire l'image via le miroir et liste les démons concernés
ceph orch upgrade check quay.io/ceph/ceph:v21.2.1

# 4. Lancer et suivre
ceph orch upgrade start --image quay.io/ceph/ceph:v21.2.1
ceph orch upgrade status
ceph -W cephadm
```

- **Image manquante pendant l'upgrade** : l'upgrade se met en pause (`manifest unknown` sur le
  registre local). Copiez l'image manquante avec `mirror.sh`, puis `ceph orch upgrade resume`.
- **Saut de version** : cephadm accepte au plus 2 releases majeures d'écart (ex. Squid → Umbrella).
  Au-delà, passez par une release intermédiaire.
- `ceph orch upgrade ls <registre-local>/quay.io/ceph/ceph` ne fonctionne qu'avec un registre local
  en **HTTPS avec un certificat reconnu par le conteneur mgr**. Le mgr n'accepte pas le HTTP.
- `--version` fonctionne de Reef (18.x) à aujourd'hui. Pour une version précise, la liste vient du
  code source au tag correspondant sur GitHub (le dépôt APT ne contient que la dernière version de
  chaque release).

## Bon à savoir

- **Toujours passer `--image`.** Le `cephadm` publié garde `DEFAULT_IMAGE = quay.ceph.io/ceph-ci/ceph:main`,
  une image de développement. Sans `--image`, le bootstrap tenterait de la tirer.
  `registries.conf` redirige aussi `quay.ceph.io` par précaution.
- **Synchronisation horaire.** cephadm exige un service NTP actif. Sans source NTP sur le réseau air-gap,
  configurez chrony avec `local stratum 10` ou pointez-le vers un serveur NTP interne.
- **Le paquet `cephadm` de Debian n'est pas celui de Ceph.** Debian trixie fournit cephadm 18.2.x (Reef).
  Utilisez le dépôt `download.ceph.com`.
- **Tags non figés.** Certaines images utilisent des tags mobiles (`grafana/alloy:latest`, `haproxy:2.3`).
  Pour un résultat reproductible, figez les digests une fois copiées dans le registre local.
- Une fois le cluster créé, chaque image peut être remplacée par
  `ceph config set mgr mgr/cephadm/container_image_<service> <image>`.
