#!/bin/sh
# Run a build step trusting the optional `ca_bundle` build secret, for builds behind a
# TLS-intercepting proxy. Without the secret the default roots apply, as in CI.
set -eu
if [ -f /run/secrets/ca_bundle ]; then
    export SSL_CERT_FILE=/run/secrets/ca_bundle
fi
exec "$@"
