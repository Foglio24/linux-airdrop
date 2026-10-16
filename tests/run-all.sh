#!/bin/sh
# Runs every test file in its own process (the GTK tests need one application per process). Exit status 1 if any fails.
cd "$(dirname "$0")/.." || exit 1
failed=0
for t in tests/test_*.py; do
    printf '%-38s' "$t"
    if out=$(timeout 600 /usr/bin/python3 -I -B "$t" 2>&1); then
        echo "$out" | grep -E '^(Ran|OK|FAILED)' | tr '\n' ' '; echo
    else
        echo "FAILED"; echo "$out" | tail -20; failed=1
    fi
done
exit $failed
