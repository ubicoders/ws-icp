# Zenoh Router: Full-Capability Example

The current `docker-compose.yml` runs a minimal router: TCP/UDP/WebSocket listeners plus the REST plugin.
This document shows what a "fully loaded" `zenohd` looks like, driven by a config file instead of CLI flags.

Everything below was tested against `eclipse/zenoh:latest` = **zenohd v1.8.0**.

## Layout

```
.
├── docker-compose.full.yml
└── zenoh/
    ├── zenoh.json5        # router configuration
    ├── credentials.txt    # user:password dictionary (optional)
    └── certs/             # TLS material (optional)
        ├── ca.pem
        ├── cert.pem
        └── key.pem
```

## docker-compose.full.yml

```yaml
services:
  zenoh-router:
    image: eclipse/zenoh:1.8.0          # pin the version; "latest" moves
    network_mode: "host"                # needed for multicast scouting + simple port handling
    restart: unless-stopped
    environment:
      RUST_LOG: info                    # debug / trace for troubleshooting
    volumes:
      - ./zenoh/zenoh.json5:/zenoh.json5:ro
      - ./zenoh/certs:/certs:ro
      - ./zenoh/credentials.txt:/credentials.txt:ro
    command: [ "-c", "/zenoh.json5" ]
    healthcheck:
      test: [ "CMD", "nc", "-z", "localhost", "7447" ]
      interval: 2s
      timeout: 2s
      retries: 5
```

CLI flags still work alongside `-c` and override the file, e.g. `--cfg='plugins/rest/http_port:9000'`.

## zenoh/zenoh.json5

```json5
{
  // ---------------------------------------------------------------- identity
  mode: "router",
  // Fixed ID so the router keeps the same ZID across restarts (32 hex chars max).
  // Omit to get a random one each start.
  id: "a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5",
  metadata: { name: "ws-icp-router" },

  // -------------------------------------------------------------- listeners
  listen: {
    endpoints: [
      "tcp/[::]:7447",                  // default transport
      "udp/[::]:7447",
      "ws/[::]:8000",                   // native zenoh protocol over WebSocket (NOT for browsers, see notes)
      "tls/[::]:7448",                  // needs transport.link.tls below
      "quic/[::]:7449",                 // needs the same TLS material
      "unixsock-stream//tmp/zenoh.sock" // local IPC on the host
    ],
    exit_on_failure: true,              // fail fast instead of running half-configured
  },

  // Routers to mesh with (other sites, cloud router, ...). Empty for a single router.
  connect: { endpoints: [] },

  // -------------------------------------------------------------- discovery
  scouting: {
    multicast: {
      enabled: true,
      address: "224.0.0.224:7446",
      interface: "auto",
      autoconnect: { router: [], peer: ["router", "peer"] },
    },
    gossip: {
      enabled: true,
      multihop: false,
      autoconnect: { router: [], peer: ["router", "peer"] },
    },
  },

  // ------------------------------------------------------------- behaviour
  timestamping: {
    enabled: { router: true, peer: false, client: false },
    drop_future_timestamp: false,
  },
  queries_default_timeout: 10000,

  // Admin space is exposed under @/<zid>/router/** (and @/local/router via REST).
  // write:true lets you change config at runtime through PUT on the REST API.
  adminspace: { enabled: true, permissions: { read: true, write: true } },

  // --------------------------------------------------------------- plugins
  // The image ships libzenoh_plugin_rest.so and libzenoh_plugin_storage_manager.so in /.
  plugins_loading: { enabled: true, search_dirs: ["/", "/plugins"] },
  plugins: {
    rest: { http_port: 8001 },

    storage_manager: {
      // "memory" is built in. Other volumes (filesystem, rocksdb, influxdb, s3)
      // need their own libzenoh_backend_*.so mounted into a search dir.
      // volumes: { fs: { __path__: "/plugins/libzenoh_backend_fs.so" } },
      storages: {
        demo:  { key_expr: "demo/**",          volume: "memory" },
        state: { key_expr: "robot/*/state",    volume: "memory" },
        // persistent example (requires the fs backend above):
        // logs: { key_expr: "robot/*/log/**", volume: { id: "fs", dir: "logs" } },
      },
    },
  },

  // -------------------------------------------------------------- transport
  transport: {
    unicast: {
      max_links: 1,
      max_sessions: 1000,
      lowlatency: false,
      qos: { enabled: true },
      compression: { enabled: false },
    },
    link: {
      tx: { batch_size: 65535, keep_alive: 4, lease: 10000 },
      rx: { buffer_size: 65535, max_message_size: 1073741824 },
      tls: {
        root_ca_certificate: "/certs/ca.pem",
        listen_private_key:  "/certs/key.pem",
        listen_certificate:  "/certs/cert.pem",
        enable_mtls: false,               // true = clients must present a cert too
        verify_name_on_connect: true,
      },
    },
    auth: {
      // Clients authenticate with user/password; file format is one "user:password" per line.
      usrpwd: { dictionary_file: "/credentials.txt" },
      // pubkey: { known_keys_file: "/certs/known_keys.pem" },
    },
    shared_memory: { enabled: true },
  },

  // ---------------------------------------------------------- access control
  // Flip enabled:true to enforce. With default_permission:"deny" everything not
  // matched by a policy is dropped.
  access_control: {
    enabled: false,
    default_permission: "deny",
    rules: [
      {
        id: "demo-rw",
        permission: "allow",
        flows: ["ingress", "egress"],
        key_exprs: ["demo/**", "robot/**"],
        messages: [
          "put", "delete", "declare_subscriber", "query", "reply",
          "liveliness_token", "declare_liveliness_subscriber", "liveliness_query",
        ],
      },
    ],
    subjects: [
      // match on any combination of usernames / cert_common_names / interfaces / link_protocols
      { id: "workshop-users", usernames: ["workshop", "robot"] },
    ],
    policies: [
      { rules: ["demo-rw"], subjects: ["workshop-users"] },
    ],
  },
}
```

## zenoh/credentials.txt

```
workshop:workshop-secret
robot:robot-secret
```

## Generating the TLS material

Self-signed, good enough for a workshop LAN. Replace the SAN list with the router's real hostname/IP.

```bash
mkdir -p zenoh/certs && cd zenoh/certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout key.pem -out cert.pem \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
cp cert.pem ca.pem      # clients trust this as root CA
```

Clients connecting over `tls/` or `quic/` must set `transport.link.tls.root_ca_certificate` to `ca.pem`.

## Bringing it up and checking it

```bash
docker compose -f docker-compose.full.yml up -d
docker compose -f docker-compose.full.yml logs -f zenoh-router   # look for "Zenoh can be reached at:"

# router info, plugins, sessions, locators
curl -s http://localhost:8001/@/local/router | jq

# storage round trip through REST
curl -X PUT -d 'hello' http://localhost:8001/demo/a
curl http://localhost:8001/demo/a

# runtime config change (adminspace write:true)
curl -X PUT -H 'content-type: application/json' \
  -d '{"key_expr":"sensor/**","volume":"memory"}' \
  http://localhost:8001/@/local/router/config/plugins/storage_manager/storages/sensor
```

Expected locators after start: `tcp`, `udp`, `ws`, `tls`, `quic`, `unixsock-stream`.
Expected plugins: `rest`, `storage_manager`.

## What each piece buys you

| Feature | Where | Needed for |
|---|---|---|
| Fixed `id` | top level | stable ZID in logs/admin space across restarts |
| `connect.endpoints` | top level | meshing several routers (multi-site, cloud bridge) |
| `scouting.multicast` | `scouting` | clients find the router with no explicit `connect` (host networking required) |
| `adminspace.permissions.write` | `adminspace` | hot-reconfigure via REST or `z_put` on `@/...` keys |
| `storage_manager` | `plugins` | replies to `get()` from stored data, last-value cache, history |
| `tls/` + `quic/` listeners | `listen`, `transport.link.tls` | encrypted links across untrusted networks |
| `usrpwd` / `pubkey` auth | `transport.auth` | identify clients, prerequisite for ACL by username |
| `access_control` | top level | per-key-expression allow/deny per client |
| `unixsock-stream` | `listen` | zero-copy-ish local IPC for processes on the router host |
| `shared_memory` | `transport` | SHM transport for same-host peers |

## Notes and gotchas

- **Browser clients.** The `ws/` listener speaks the native zenoh protocol over WebSocket. The `zenoh-ts` library does not use it; it needs the separate `zenoh-plugin-remote-api` (default port 10000), which is not in the `eclipse/zenoh` image. Build or pull a router image that includes that plugin if you want web clients.
- **Persistence.** Only the `memory` volume is bundled. Data is lost on restart unless you mount a backend `.so` (filesystem, rocksdb, influxdb) and point a storage at it.
- **ACL and auth go together.** `access_control.subjects` matching on `usernames` only works when `transport.auth.usrpwd` is configured and the client actually authenticates.
- **`exit_on_failure: true`** makes the container die if any listener cannot bind. That is what you want with `restart: unless-stopped`; a half-up router is worse than a restarting one.
- **Host networking** is what makes multicast scouting and the `[::]` dual-stack binds work. If you switch to bridge networking, publish each port explicitly and clients must `connect` to the router address.
- **Full schema.** Every key above comes from the router's own default config. To see all of it: `docker run --rm eclipse/zenoh:1.8.0 2>&1 | grep 'Initial conf'`, or `curl localhost:8001/@/local/router/config`.
