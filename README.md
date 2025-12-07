# Thingie

Ensure sops is installed, and that you have `secrets/age.key` present

```
source .env
talhelper genconfig \
    --config-file talos/talconfig.yaml \
    --no-gitignore \
    --out-dir talos/.rendered \
    --secret-file talos/talsecret.enc.yaml
```
