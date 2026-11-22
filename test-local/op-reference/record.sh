#!/usr/bin/env bash
# Records what the real `op` returns for every form Casa and the finance
# plugin run, against a test vault. All values are made up here. Every
# item it creates carries the tag below and is deleted at the end.
#
#   OP_REFERENCE_VAULT  name of a vault that may be written to (a test vault)
#   OP_REFERENCE_BIN    path of the `op` binary to record
#   OP_SERVICE_ACCOUNT_TOKEN (or a signed-in session) must be set for `op`
#
# Output goes to ./raw next to this script. Before comparing with
# tests/fixtures/op_reference, replace the vault name, the vault id and the
# user id in the output with the placeholders the fixtures use.
set -u
: "${OP_REFERENCE_VAULT:?set OP_REFERENCE_VAULT to the test vault name}"
: "${OP_REFERENCE_BIN:?set OP_REFERENCE_BIN to the op binary path}"
cd "$(dirname "$0")"
OP="$OP_REFERENCE_BIN"
OUT="$PWD/raw"
V="$OP_REFERENCE_VAULT"
TAG="casa-vault-fixture"
A="zz-vault-fixture-a"; B="zz-vault-fixture-b"; C="zz-vault-fixture-c"
rm -rf "$OUT"; mkdir -p "$OUT"

rec() {  # rec <name> [--stdin <text>] -- <op args...>
    local name="$1"; shift
    local stdin=""
    local has_stdin=0
    if [ "$1" = "--stdin" ]; then stdin="$2"; has_stdin=1; shift 2; fi
    shift  # --
    printf '%s\n' "$@" > "$OUT/$name.args"
    if [ "$has_stdin" = 1 ]; then
        printf '%s' "$stdin" > "$OUT/$name.stdin"
        printf '%s' "$stdin" | "$OP" "$@" > "$OUT/$name.out" 2> "$OUT/$name.err"
    else
        "$OP" "$@" > "$OUT/$name.out" 2> "$OUT/$name.err" < /dev/null
    fi
    echo $? > "$OUT/$name.code"
    echo "$name exit=$(cat "$OUT/$name.code") out=$(wc -c < "$OUT/$name.out")b err=$(wc -c < "$OUT/$name.err")b"
}
id_of() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$OUT/$1.out"; }

TPL1='{"title": "'$A'", "category": "PASSWORD", "tags": ["'$TAG'"], "fields": [{"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password", "value": "fixture-value-one"}]}'
TPL2='{"title": "'$A'", "category": "PASSWORD", "tags": ["'$TAG'"], "fields": [{"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password", "value": "fixture-value-two"}]}'

rec 01-version -- --version
rec 02-create-template --stdin "$TPL1" -- item create --vault "$V" --format json -
IDA="$(id_of 02-create-template)"
rec 03-list-vault -- item list --vault "$V" --format json
rec 04-list-tags-archive -- item list --vault "$V" --tags "$TAG" --include-archive --format json
rec 05-get-id-json -- item get "$IDA" --vault "$V" --format json
rec 06-get-title-json -- item get "$A" --vault "$V" --format json
rec 07-get-fields-reveal -- item get "$IDA" --vault "$V" --fields label=password --reveal
rec 08-read -- read "op://$V/$A/password"
rec 09-edit-template --stdin "$TPL2" -- item edit "$IDA" --vault "$V" --format json
rec 10-read-after-edit -- read "op://$V/$A/password"
rec 11-create-api-credential -- item create --category "API Credential" --title "$B" --vault "$V" --tags "$TAG" "refresh token[password]=fixture-refresh-one"
rec 12-edit-assign-existing -- item edit "$B" --vault "$V" "refresh token[password]=fixture-refresh-two"
rec 13-edit-assign-new-field -- item edit "$B" --vault "$V" "session note[text]=fixture plain text"
rec 14-read-added-field -- read "op://$V/$B/session note"
rec 15-get-b-json -- item get "$B" --vault "$V" --format json
rec 16-create-ssh -- item create --category ssh --title "$C" --vault "$V" --tags "$TAG" --ssh-generate-key rsa,4096
rec 17-read-private-key -- read "op://$V/$C/private key"
rec 18-get-c-json -- item get "$C" --vault "$V" --format json
rec 19-list-tags-three -- item list --vault "$V" --tags "$TAG" --include-archive --format json
# error cases
rec 30-read-missing-item -- read "op://$V/zz-vault-fixture-none/password"
rec 31-read-missing-field -- read "op://$V/$A/nope"
rec 32-get-missing-item -- item get "zz-vault-fixture-none" --vault "$V" --format json
rec 33-edit-missing-item -- item edit "zz-vault-fixture-none" --vault "$V" "x[password]=y"
rec 34-delete-missing-item -- item delete "zz-vault-fixture-none" --vault "$V"
rec 35-get-fields-missing -- item get "$IDA" --vault "$V" --fields label=nope --reveal
rec 36-read-wrong-vault -- read "op://zz-no-such-vault/$A/password"
rec 37-list-wrong-vault -- item list --vault "zz-no-such-vault" --format json
rec 38-list-empty-tag -- item list --vault "$V" --tags "zz-no-such-tag" --include-archive --format json
# two items with one title
rec 40-create-duplicate-title --stdin "$TPL1" -- item create --vault "$V" --format json -
rec 41-get-duplicate-title -- item get "$A" --vault "$V" --format json
rec 42-read-duplicate-title -- read "op://$V/$A/password"
rec 43-edit-duplicate-title -- item edit "$A" --vault "$V" "x[password]=y"
# deletes (also the clean-up)
rec 50-delete-id -- item delete "$IDA" --vault "$V"
rec 51-read-after-delete -- read "op://$V/$B/refresh token"
for id in $("$OP" item list --vault "$V" --tags "$TAG" --include-archive --format json | python3 -c 'import json,sys; print(" ".join(r["id"] for r in json.load(sys.stdin)))'); do
    "$OP" item delete "$id" --vault "$V" && echo "cleaned $id"
done
echo "left with tag: $("$OP" item list --vault "$V" --tags "$TAG" --include-archive --format json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
