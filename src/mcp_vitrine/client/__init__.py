"""The ``vitrine`` client: a token holder's command line over ``/api`` (spec § 9).

It is not the operator's ``mcp-vitrine``: it never reads ``data_dir`` or ``VITRINE_SECRET``, and
it imports nothing from the server's layers (an import-linter contract holds that), so it runs on
a laptop or in CI with only a URL and a token.
"""
