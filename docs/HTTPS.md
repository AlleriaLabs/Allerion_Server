# HTTPS access with Caddy

For remote access, prefer a VPN. If you need a public HTTPS endpoint, use a domain whose DNS A/AAAA records point to your server and forward TCP ports 80 and 443 to it.

1. Copy examples/Caddyfile to Caddyfile in the Allerion installation directory.
2. Replace allerion.example.com with your real DNS name.
3. Start Caddy using the separate Compose file below (the Allerion API remains on its existing host-network port).

Create docker-compose.https.yml:

    services:
      caddy:
        image: caddy:2
        container_name: allerion-caddy
        restart: unless-stopped
        network_mode: host
        volumes:
          - ./Caddyfile:/etc/caddy/Caddyfile:ro
          - ./caddy_data:/data
          - ./caddy_config:/config

Then run:

    docker compose -f docker-compose.yml -f docker-compose.https.yml up -d

Caddy obtains and renews certificates automatically when public DNS and inbound ports are correct. Keep caddy_data/ and caddy_config/ persistent. Do not expose the Docker socket proxy. Connect the Android app to https://your-domain.example.com.

If ports 80/443 cannot be forwarded or the host is behind CGNAT, use a private VPN instead. Do not use plain HTTP password sign-in over an untrusted network.
