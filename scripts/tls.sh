#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]]; then
  echo "Usage: $0 <dashboard IPv4 or DNS name>" >&2
  exit 2
fi

host=$1
dir=tls-fluxo
cert=$dir/cert.pem
key=$dir/key.pem
trust=$cert
if [ -f "$dir/ca.pem" ]; then
  trust=$dir/ca.pem
fi

if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  san="IP:$host"
  identity=(-verify_ip "$host")
else
  san="DNS:$host"
  identity=(-verify_hostname "$host")
fi

if [ ! -e "$cert" ] && [ ! -e "$key" ] && [ "$trust" = "$cert" ]; then
  mkdir -p -m 700 "$dir"
  tmp=$(mktemp -d "$dir/.new.XXXXXX")
  trap 'rm -rf "$tmp"' EXIT
  (
    umask 077
    openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 365 \
      -keyout "$tmp/key.pem" -out "$tmp/cert.pem" -subj "/CN=$host" \
      -addext "subjectAltName=$san" \
      -addext 'basicConstraints=critical,CA:FALSE' \
      -addext 'extendedKeyUsage=serverAuth'
  )
  openssl verify -CAfile "$tmp/cert.pem" -purpose sslserver "${identity[@]}" "$tmp/cert.pem"
  mv "$tmp/key.pem" "$key"
  mv "$tmp/cert.pem" "$cert"
fi

if [ ! -f "$cert" ] || [ ! -f "$key" ]; then
  echo "Place the certificate and unencrypted PEM key in $dir before continuing." >&2
  exit 1
fi

cert_key=$(openssl x509 -in "$cert" -noout -pubkey | openssl pkey -pubin -outform DER | openssl dgst -sha256)
private_key=$(openssl pkey -in "$key" -passin pass: -pubout -outform DER | openssl dgst -sha256)
if [ "$cert_key" != "$private_key" ]; then
  echo "The certificate and private key do not match." >&2
  exit 1
fi

verify_options=(-CAfile "$trust" -purpose sslserver)
if [ "$trust" != "$cert" ]; then
  verify_options+=(-untrusted "$cert")
fi
openssl verify "${verify_options[@]}" "${identity[@]}" "$cert"
group=${TLS_GID:-10001}
if ! chgrp "$group" "$dir" "$key" 2>/dev/null; then
  sudo chgrp "$group" "$dir" "$key"
fi
chmod 750 "$dir"
chmod 640 "$key"
chmod 644 "$cert" "$trust"
openssl x509 -in "$cert" -noout -subject -enddate
printf 'TLS verified for %s. Trust file: %s\n' "$host" "$trust"
