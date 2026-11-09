"""On-demand PTR resolution through the configured private or system resolver."""
from functools import lru_cache
from ipaddress import ip_address
import os
import re

import dns.exception
import dns.resolver


@lru_cache(maxsize=16)
def resolver_for(server):
    resolver = dns.resolver.Resolver(configure=server is None)
    if server is not None:
        resolver.nameservers = [server]
    resolver.timeout = 1.5
    resolver.cache = dns.resolver.LRUCache(512)
    return resolver


def configured_resolvers():
    values = re.split(r"[\s,]+", os.environ.get("PTR_DNS", "").strip())
    addresses = dict.fromkeys(str(ip_address(value)) for value in values if value)
    return tuple(resolver_for(server) for server in addresses) or (resolver_for(None),)


def lookup(address, resolvers):
    failures = []
    for resolver in resolvers:
        try:
            return str(resolver.resolve_address(address, lifetime=2.5)[0].target).rstrip(".")
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except dns.exception.DNSException as exc:
            failures.append(exc)
    if failures:
        raise failures[-1]
    return None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test PTR lookup from the collector.")
    parser.add_argument("address", type=ip_address)
    args = parser.parse_args()
    try:
        resolvers = configured_resolvers()
        print("Resolvers:", ", ".join(str(server) for resolver in resolvers for server in resolver.nameservers))
        print(lookup(str(args.address), resolvers) or "No PTR record")
    except (ValueError, dns.exception.DNSException) as exc:
        parser.exit(1, f"PTR lookup failed: {exc}\n")
