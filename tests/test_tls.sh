#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
export TLS_GID
TLS_GID=$(id -g)

bash "$root/scripts/tls.sh" 192.0.2.20 >/dev/null 2>&1
before=$(openssl x509 -in tls-fluxo/cert.pem -noout -fingerprint -sha256)
bash "$root/scripts/tls.sh" 192.0.2.20 >/dev/null
test "$before" = "$(openssl x509 -in tls-fluxo/cert.pem -noout -fingerprint -sha256)"
if bash "$root/scripts/tls.sh" 192.0.2.21 >/dev/null 2>&1; then
  echo "TLS setup accepted a mismatched address."
  exit 1
fi
test "$before" = "$(openssl x509 -in tls-fluxo/cert.pem -noout -fingerprint -sha256)"

mkdir dns
cd dns
bash "$root/scripts/tls.sh" fluxo.example.test >/dev/null 2>&1
openssl verify -CAfile tls-fluxo/cert.pem -verify_hostname fluxo.example.test \
  tls-fluxo/cert.pem >/dev/null
# Reusing the certificate with a different key must fail.
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
  -out tls-fluxo/key.pem >/dev/null 2>&1
if bash "$root/scripts/tls.sh" fluxo.example.test >/dev/null 2>&1; then
  echo "TLS setup accepted a mismatched key."
  exit 1
fi

mkdir ../ca
cd ../ca
mkdir tls-fluxo
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj '/CN=Fluxo test CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -keyout ca.key -out tls-fluxo/ca.pem >/dev/null 2>&1
openssl req -new -newkey rsa:2048 -nodes -subj '/CN=fluxo.example.test' \
  -keyout tls-fluxo/key.pem -out leaf.csr >/dev/null 2>&1
printf '%s\n' 'basicConstraints=critical,CA:FALSE' 'subjectAltName=DNS:fluxo.example.test' \
  'extendedKeyUsage=serverAuth' > leaf.ext
openssl x509 -req -in leaf.csr -CA tls-fluxo/ca.pem -CAkey ca.key -CAcreateserial \
  -days 2 -extfile leaf.ext -out tls-fluxo/cert.pem >/dev/null 2>&1
bash "$root/scripts/tls.sh" fluxo.example.test >/dev/null
echo "TLS generation, reuse, name and key checks, and private CA verification passed."
