# Sealed Bundle Transport

Sealed `.scap` envelopes provide local confidentiality for bundles that are carried through user-controlled storage, a local gateway bundle store, or a Model Plane UI.

The repo does not implement cryptographic primitives. It delegates encryption and decryption to an external age-compatible command and wraps the encrypted payload in an inspectable `.scap` envelope.

## Recommended Backend

Use the `age` CLI or an age-compatible executable.

The command path is configurable:

```powershell
py -3 .\scripts\capsule_cli.py seal .\thread.scap --out .\thread.sealed.scap --age-bin age --age-recipient-file .\.capsules\security\recipients\local.agepub
py -3 .\scripts\capsule_cli.py unseal .\thread.sealed.scap --out .\thread.unsealed.scap --age-bin age --age-identity C:\Users\you\.config\age\keys.txt
```

## Key Handling

Recommended v0 policy:

- Age recipients are public key material and may be stored as project launch policy.
- Age identity files are private keys and should stay outside `.capsules/`.
- Job packets, gateway launch profiles, and `.scap` bundles must not contain identity key values.
- Store secret references or operator-local paths, not secrets.
- Keep signing keys and age identities separate. HMAC signing proves shared-key authenticity; age sealing provides confidentiality.
- Model Plane launch profiles may include `security.bundle_sealing.age_recipient_file` for public recipient policy.

A reasonable project-local public recipient path is:

```text
.capsules/security/recipients/local.agepub
```

A reasonable private identity path is outside capsule state:

```text
C:\Users\you\.config\age\keys.txt
```

## Model Plane Profile Policy

The launch profile can advertise the public sealing policy for upload/download controls:

```json
{
  "security": {
    "bundle_sealing": {
      "enabled": true,
      "age_bin": "age",
      "age_recipient_file": ".capsules/security/recipients/local.agepub",
      "require_for_external_transfer": true
    }
  }
}
```

`gateway command --json` turns that into `bundle_sealing.seal_command_template`. The template is for Model Plane or an operator before upload/download transfer. It is not a gateway runtime flag, and it never contains an age identity.

## Seal

Seal with an inline recipient:

```powershell
py -3 .\scripts\capsule_cli.py seal .\research-loop.scap --out .\research-loop.sealed.scap --age-recipient age1...
```

Seal with a recipient file:

```powershell
py -3 .\scripts\capsule_cli.py seal .\research-loop.scap --out .\research-loop.sealed.scap --age-recipient-file .\.capsules\security\recipients\local.agepub
```

Check the envelope before sharing:

```powershell
py -3 .\scripts\capsule_cli.py inspect --bundle .\research-loop.sealed.scap --json
py -3 .\scripts\capsule_cli.py bundle-policy .\research-loop.sealed.scap --preset sealed
```

## Store

Store a sealed bundle in the gateway bundle store without importing it:

```powershell
py -3 .\scripts\capsule_cli.py gateway store --url http://127.0.0.1:8765 --bundle .\research-loop.sealed.scap --bundle-id research-loop-sealed --policy-preset sealed --auth-token-file .\capsule-gateway-token
```

Model Plane can use the same policy through `gateway_store_bundle` job packets:

```json
{
  "job_type": "gateway_store_bundle",
  "params": {
    "gateway_url": "http://127.0.0.1:8765",
    "bundle": "research-loop.sealed.scap",
    "bundle_id": "research-loop-sealed",
    "policy_preset": "sealed"
  }
}
```

## Unseal And Import

Sealed envelopes are not imported directly. Unseal first:

```powershell
py -3 .\scripts\capsule_cli.py unseal .\research-loop.sealed.scap --out .\research-loop.unsealed.scap --age-identity C:\Users\you\.config\age\keys.txt
py -3 .\scripts\capsule_cli.py import .\research-loop.unsealed.scap --thread-id research-loop-copy
```

## Boundary

Implemented now:

- local sealed `.scap` envelopes
- external age-compatible encryption command
- digest verification of sealed and unsealed payloads
- gateway/store policy checks with `--policy-preset sealed`

Not implemented yet:

- hosted provider-side sealed capsules
- provider-issued resume blobs
- user-carried runtime snapshots that are portable across model backends
