"""The image's HEALTHCHECK: ``GET /healthz`` with a ``Host`` the server accepts.

Every route checks ``Host`` (spec § 7.4), so the probe sends the one ``DROP_PUBLIC_BASE_URL``
names rather than ``127.0.0.1``. The image ships no curl; the standard library is enough.
"""

import os
import sys
import urllib.request
from urllib.parse import urlsplit

TIMEOUT = 3


def main() -> int:
    """Exit 0 when ``/healthz`` answers 200."""
    host = urlsplit(os.environ.get("DROP_PUBLIC_BASE_URL", "http://localhost:8080")).netloc
    port = os.environ.get("DROP_PORT", "8080")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/healthz", headers={"Host": host})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return 0 if response.status == 200 else 1  # noqa: PLR2004
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
