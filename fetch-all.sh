#!/usr/bin/env bash
# Fetch all entity CSVs from QLever Wikidata SPARQL endpoint.
# Skips files that already exist.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENDPOINT="https://qlever.dev/api/wikidata"
mkdir -p results

for rq_file in queries/*.rq; do
    entity="$(basename "$rq_file" .rq)"
    out="results/${entity}.csv"

    if [ -f "$out" ]; then
        echo "Skipping ${entity} (${out} already exists)"
        continue
    fi

    echo "Fetching ${entity}..."
    curl -s -X POST "$ENDPOINT" \
        -H "Content-Type: application/sparql-query" \
        -H "Accept: text/csv" \
        --data-binary "@${rq_file}" \
        > "$out"
    echo "  → ${out}"
done

echo "All fetches complete."
