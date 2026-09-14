#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# This host-owned file survives checkout and may select existing model caches.
settings="${OCR_DEPLOY_ENV:-$HOME/.config/sir-ocr/deploy.env}"
if [[ -f "$settings" ]]; then
  set -a
  source "$settings"
  set +a
fi
export OCR_DATA_DIR="${OCR_DATA_DIR:-$HOME/.local/share/sir-ocr/data}"
export OCR_MODEL_CACHE="${OCR_MODEL_CACHE:-$HOME/.local/share/sir-ocr/cache/paddlex}"
export OCR_AUX_CACHE="${OCR_AUX_CACHE:-$HOME/.local/share/sir-ocr/cache/aux}"
export OCR_UID="$(id -u)" OCR_GID="$(id -g)"
export OCR_PORT="${OCR_PORT:-8096}"
export OCR_IMAGE_TAG="${OCR_IMAGE_TAG:-${GITHUB_SHA:-latest}}"
mkdir -p "$OCR_DATA_DIR" "$OCR_MODEL_CACHE" "$OCR_AUX_CACHE/home"
docker network inspect sir-server_sir-net >/dev/null
# Pin trust to the real proxy hops; do not trust the entire Docker subnet.
nginx_ip=$(docker inspect sir-nginx --format '{{(index .NetworkSettings.Networks "sir-server_sir-net").IPAddress}}')
tunnel_ip=$(docker inspect cloudflared --format '{{(index .NetworkSettings.Networks "sir-server_sir-net").IPAddress}}')
[[ -n "$nginx_ip" && -n "$tunnel_ip" ]]
export OCR_TRUSTED_PROXIES="$nginx_ip/32,$tunnel_ip/32"
# sir-mcp forwards uploads with the client's X-Forwarded-For; trust it so limits stay per real client.
# ponytail: IP is read at deploy time; after sir-mcp is recreated, redeploy sir-ocr (until then MCP uploads share one limit).
mcp_ip=$(docker inspect sir-mcp-mcp-1 --format '{{(index .NetworkSettings.Networks "sir-server_sir-net").IPAddress}}' 2>/dev/null || true)
if [[ -n "$mcp_ip" ]]; then
  OCR_TRUSTED_PROXIES+=",$mcp_ip/32"
fi
compose=(docker compose -f compose.yaml -f compose.sir.yaml)
"${compose[@]}" config --quiet
case "${1:-deploy}" in
  build)
    "${compose[@]}" build
    ;;
  deploy)
    exec 9>/tmp/sir-deploy.lock
    flock --wait 600 9
    "${compose[@]}" up -d --no-build --remove-orphans --wait --wait-timeout 120
    # Assert the shared gateway supports this service's upload label.
    for attempt in $(seq 1 30); do
      if docker exec sir-nginx sh -c 'cat /etc/nginx/conf.d/sir-ocr-api-1.conf' 2>/dev/null | grep -q 'client_max_body_size 51m;'; then
        break
      fi
      if [[ "$attempt" == 30 ]]; then
        echo 'Gateway must support proxy.max_body_size (sir-server b10de81 or later).' >&2
        exit 1
      fi
      sleep 2
    done
    curl --fail --silent --show-error --retry 12 --retry-all-errors --retry-delay 2 https://ocr.sir-labs.com/healthz
    ;;
  *) echo 'Usage: scripts/deploy.sh [build|deploy]' >&2; exit 2 ;;
esac
