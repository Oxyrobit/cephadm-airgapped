#!/usr/bin/env bash
# Côté machine connectée : copie des images publiques vers le registre local.
# Usage : ./mirror.sh <registre-local> <image>...
#   ./mirror.sh registry.local:5000 $(cat images.txt)
#   ./mirror.sh registry.local:5000 $(./ceph-images.py)
# Prérequis : skopeo (ou docker/podman : SKOPEO="docker run --rm --net host quay.io/skopeo/stable:v1.20.0").
# Destination : <registre-local>/<registre-d'origine>/<chemin>:<tag>, cohérent avec registries.conf.
set -euo pipefail
SKOPEO=${SKOPEO:-skopeo}
DEST_TLS_VERIFY=${DEST_TLS_VERIFY:-false}
ARCH=${ARCH:-amd64}
[ $# -ge 2 ] || { sed -n '2,7p' "$0"; exit 2; }
dest_reg=$1; shift
for src in "$@"; do
  reg=${src%%/*}; rest=${src#*/}
  [[ $reg == *.* || $reg == *:* ]] || { reg=docker.io; rest=$src; }
  [[ $reg == docker.io && $rest != */* ]] && rest=library/$rest
  echo ">> $reg/$rest -> $dest_reg/$reg/$rest"
  $SKOPEO copy --override-arch "$ARCH" --override-os linux \
    --dest-tls-verify="$DEST_TLS_VERIFY" "docker://$reg/$rest" "docker://$dest_reg/$reg/$rest"
done
