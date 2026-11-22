# op reference recorder

`record.sh` runs every `op` form Casa and the finance plugin use against a
test vault and writes one file set per case (`NN-name.args`, `.stdin`,
`.out`, `.err`, `.code`) into `raw/`. The sanitized copy lives in
`tests/fixtures/op_reference/` and is replayed by the tests.

Run it whenever the `OP_CLI_VERSION` pin in `casa/Dockerfile` changes:

    OP_REFERENCE_VAULT="<test vault>" OP_REFERENCE_BIN=/path/to/op ./record.sh

Use a vault that holds nothing you care about: the script creates and deletes
items tagged `casa-vault-fixture`. Then replace the vault name, vault id and
user id in `raw/` with the placeholders used in the fixtures (vault name
`Casa`, vault id `v` x 26, user id `U` x 26), diff `raw/` against
`tests/fixtures/op_reference/`, and review every difference before updating
the fixtures. Timestamps and item ids differ on every run.

The SSH cases also hold a real generated key. Before anything leaves `raw/`,
mask it as the current fixtures do: the private key in both formats
(the PKCS#8 PEM block and the OpenSSH private key block, each with the body
replaced by `REDACTED`), the public key
(`ssh-rsa REDACTED`) and the fingerprint (`SHA256:REDACTED`). `raw/` is
ignored by git, so none of this can be committed by accident.

`03-list-vault` is a filtered copy, not a recording, and always differs from
`raw/`: its `.note` file says how it was made from the raw output.
