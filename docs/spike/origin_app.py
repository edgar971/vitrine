import drop_spike
# The production config: Host/Origin guard on, explicit allowlist (DNS-rebinding defence).
app = drop_spike.mcp.http_app(path="/mcp", host_origin_protection=True, allowed_hosts=["drop.example.com", "drop.example.com:*"])
