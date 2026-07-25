# foundry-rest-api

Self-hosted relay for the maintained Foundry REST API project. It lets the
`obojima-tools` application read and write a Foundry actor without a custom
Foundry module and without touching Foundry's database files.

- Relay: <https://github.com/ThreeHats/foundryvtt-rest-api-relay>
- Foundry module: <https://github.com/ThreeHats/foundryvtt-rest-api>
- Module manifest, for Foundry's install-by-manifest flow:
  `https://github.com/ThreeHats/foundryvtt-rest-api/releases/latest/download/module.json`

The image is pinned. Check the upstream changelog before moving it, because the
module and relay versions are expected to match.

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
