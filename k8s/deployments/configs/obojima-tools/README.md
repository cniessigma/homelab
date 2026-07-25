# obojima-tools

Alchemy tools for the Obojima campaign, backed by a Foundry VTT actor through
the `foundry-rest-api` relay.

Application source is **not** in this repository. It is in the `obojima-tools`
repository, normally at `~/obojima_tools`, which also owns the Dockerfile and
the `make` targets that build and publish the image to `docker.nies.io:5000`.

- Bump the version in that repository's `package.json`, then run `make release`.
  That builds for `linux/arm64`, pushes the tag, and rewrites the `image:` line
  in `deployment.yaml` here.
- Do not edit the image tag by hand; `make manifest` keeps it in step with the
  published image.

## Secrets

`secret.example.yaml` lists what `obojima-tools-secret` must hold. Fill it in,
encrypt with SOPS, and commit as `secret.enc.yaml`.

`FOUNDRY_RELAY_API_KEY` can only be filled in **after** the relay is running and
a key has been minted, so deploy `foundry-rest-api` first.

`OBOJIMA_GM_TOKEN` is required. Every route that returns campaign data is
GM-only, and without the token the pod answers loopback requests only, which
means nothing reaches it through the Service.
