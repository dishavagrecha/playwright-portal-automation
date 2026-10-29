#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

destination="${1:-submission.zip}"
if [[ "$destination" != /* ]]; then
  destination="$PWD/$destination"
fi

# Archive only the committed repository. Local cookies, run artifacts and the
# virtual environment are ignored by Git and never enter this ZIP.
git archive --format=zip --output="$destination" HEAD
python3 - "$destination" <<'PY'
import sys
from zipfile import ZipFile

with ZipFile(sys.argv[1]) as archive:
    forbidden = {".state", ".venv", "artifacts", ".git"}
    unsafe = [name for name in archive.namelist()
              if forbidden.intersection(name.split("/"))]
    if unsafe:
        raise SystemExit("Submission ZIP contains private workspace paths")
print(f"Created {sys.argv[1]} from committed files only")
PY
