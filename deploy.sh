#!/bin/sh
# Run from any directory. All configuration stays under the project root.
set -eu
cd "$(dirname "$0")"
mode=${1:-demo}
if [ "$#" -gt 0 ]; then shift; fi
case "$mode" in
    demo)
        exec python3 -m socialbot demo --dashboard "$@"
        ;;
    check)
        config=${1:-examples/fleet.json}
        exec python3 -m socialbot fleet --config "$config" --check
        ;;
    fleet)
        config=${1:-fleet.json}
        if [ ! -f "$config" ]; then
            echo "Copy examples/fleet.json to $config and select your rooms first." >&2
            exit 1
        fi
        exec python3 -m socialbot fleet --config "$config"
        ;;
    docker)
        if [ ! -f fleet.json ] || [ ! -f .env ]; then
            echo "Configure fleet.json and .env in the project root first." >&2
            exit 1
        fi
        docker compose -f compose.fleet.yaml build
        docker compose -f compose.fleet.yaml run --rm listeners fleet --config /app/fleet.json --check
        exec docker compose -f compose.fleet.yaml up -d
        ;;
    *)
        echo "Usage: ./deploy.sh [demo [--port NUMBER] | check [config] | fleet [config] | docker]" >&2
        exit 2
        ;;
esac
