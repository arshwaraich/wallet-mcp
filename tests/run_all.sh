#!/usr/bin/env bash
# Runs every tests/test_*.py on its own (each starts its own throwaway servers) and exits non-zero if any fails.
# No arguments are passed on (test_public_api.py --update rewrites its fixture; run that one by hand).
cd "$(dirname "$0")/.." || exit 2
py=.venv/bin/python
failed=()
for t in tests/test_*.py; do
    echo "=== $t"
    start=$SECONDS
    if out=$("$py" "$t" 2>&1); then
        echo "$out" | tail -n 1
        echo "--- ok ($((SECONDS - start))s)"
    else
        echo "$out" | grep -v '^PASS '
        echo "--- FAILED ($((SECONDS - start))s)"
        failed+=("$t")
    fi
done
echo
if ((${#failed[@]})); then
    echo "${#failed[@]} test file(s) failed: ${failed[*]}"
    exit 1
fi
echo "all test files passed"
