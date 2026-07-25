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
RELAY=http://<relay-address>:3010

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
unrestricted Foundry CRUD credential for those scopes: keep it out of browsers
and do not expose the relay publicly.

## The bridge only works while a world client is connected

The Foundry REST module runs in a **user's browser**, not in the Foundry pod. So
the relay must be reachable from whichever browser has the world open, and the
bridge is down whenever no such browser is connected.

The Service is `ClusterIP`. Change the type or add an ingress before the module
can reach it.

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
