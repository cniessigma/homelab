# foundry-rest-api

Self-hosted relay for the maintained Foundry REST API project. It lets the
`obojima-tools` application read and write a Foundry actor without a custom
Foundry module and without touching Foundry's database files.

- Relay: <https://github.com/ThreeHats/foundryvtt-rest-api-relay>
- Foundry module: <https://github.com/ThreeHats/foundryvtt-rest-api>
- Module manifest, for Foundry's install-by-manifest flow:
  `https://github.com/ThreeHats/foundryvtt-rest-api/releases/latest/download/module.json`

## The image is built here, not pulled from upstream

Upstream publishes **linux/amd64 only**, for all 81 tags. Its runtime stage
installs Google Chrome from an `arch=amd64` apt source, and Chrome has no arm64
Linux build. Chrome is only there to drive headless Foundry sessions.

This cluster is all arm64, so the image is rebuilt from upstream source with
Chrome and the web frontend removed, and pushed to `docker.nies.io:5000`. The
Dockerfile and the `make relay-push` target live in the `obojima-tools`
repository, normally at `~/obojima_tools`.

What that costs:

- headless Foundry sessions do not work. This campaign does not use them; the
  world client is a GM's own browser.
- there is no web admin UI. Mint API keys over HTTP, as below.

Check the upstream changelog before moving the version, because the module and
relay versions are expected to match. Then run `make relay-push
RELAY_VERSION=<new>` and update the `image:` line here.

## Minting an API key

There is no dashboard in this build, and every API route rejects an unscoped
key. Both steps are plain HTTP:

```bash
# Administration goes through a port-forward; the Service is ClusterIP.
#   kubectl port-forward -n foundry-rest-api svc/foundry-rest-api 3010:3010
RELAY=http://localhost:3010

# 1. Register. Returns a sessionToken, not an API key.
TOKEN=$(curl -s -X POST $RELAY/auth/register \
  -H 'content-type: application/json' \
  -d '{"email":"gm@example.com","password":"<a long password>"}' \
  | jq -r .sessionToken)

# 2. Create a scoped key. `obojima-tools` needs exactly these scopes.
curl -s -X POST $RELAY/auth/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name":"obojima-tools","scopes":["entity:read","entity:write","search","clients:read","chat:write","dnd5e"]}' \
  | jq -r .key
```

That key is `FOUNDRY_RELAY_API_KEY` in `obojima-tools-secret`. It is an
unrestricted Foundry CRUD credential for those scopes, so it must never reach a
browser bundle. `obojima-tools` holds it server side and exposes only its own
narrow API.

The relay is reachable from the internet through the Cloudflare tunnel, so the
Access application in front of it is what keeps that public hostname from being
an open door. Do not remove it.

## How the relay is reached

| Caller | Address | Why |
| --- | --- | --- |
| Foundry REST module, in a GM's browser | `wss://relay.nies.io` through the Cloudflare tunnel | The browser loads Foundry over HTTPS, and a page served over HTTPS cannot open a plaintext `ws://` socket |
| `obojima-tools` pod | `http://foundry-rest-api.foundry-rest-api.svc.cluster.local:3010` | Stays inside the cluster, so it never touches Cloudflare and needs no service token |
| Administration, such as minting a key | `kubectl port-forward` | Deliberate: see below |

The Service is **ClusterIP on purpose**. `POST /auth/register` is
unauthenticated and only rate limited, so any LAN address for this relay would
let anyone on the network register an account, mint a scoped key, and gain full
Foundry CRUD. Cloudflare Access is what stands in front of that endpoint, and a
LoadBalancer would route around it.

For the same reason, do not create a private DNS record pointing at the pod or
a node. Split-horizon DNS would also break `wss://`, because a bare address has
no certificate.

Once the key exists, set `DISABLE_REGISTRATION=true` in the deployment
environment to close registration entirely.

### Cloudflare setup

The tunnel is remotely managed: `cloudflared` runs with `TUNNEL_TOKEN` and no
config argument, so `config.yaml` in that directory is ignored and ingress
rules live in the Zero Trust dashboard. Add there:

- a public hostname `relay.nies.io` pointing at
  `http://foundry-rest-api.foundry-rest-api.svc.cluster.local:3010`;
- an Access application in front of it, as `argocd.nies.io` has.

**Authenticate the browser before the module connects.** Access protects the
WebSocket handshake as well as ordinary pages. If the browser has no
`CF_Authorization` cookie for `relay.nies.io`, the handshake is redirected to
the Access login and the module reports a failed connection rather than a
login prompt. Visit `https://relay.nies.io` once in the same browser first.

## The bridge only works while a world client is connected

The Foundry REST module runs in a **user's browser**, not in the Foundry pod. So
the relay must be reachable from whichever browser has the world open, and the
bridge is down whenever no such browser is connected. `obojima-tools` shows this
as `Foundry offline` and refuses writes; it never treats a cached read as
authoritative.

## Secrets

`secret.example.yaml` lists what `foundry-rest-api-secret` may hold. Both values
are optional: the relay generates them and persists them to
`/app/data/.secrets.env` on the volume if they are unset. Setting them keeps the
values in SOPS instead of only on a volume.

`CREDENTIALS_ENCRYPTION_KEY` must be exactly 32 bytes, as 64 hex characters or
44 base64 characters. Rotating it makes every stored Foundry credential
permanently unreadable.

## Related

`k8s/deployments/configs/obojima-tools/` consumes this relay at
`http://foundry-rest-api.foundry-rest-api.svc.cluster.local:3010`.
