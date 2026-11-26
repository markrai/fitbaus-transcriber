import ipaddress
import re
from urllib.parse import urlsplit

from .summary_provider import SummaryProviderConfig


MAX_TRUSTED_LOBO_DEVICES = 16
LOBO_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
PEM_PATTERN = re.compile(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*")


def lobo_endpoint(value):
    if not isinstance(value, str):
        raise ValueError("Invalid LOBO endpoint")
    url = urlsplit(value)
    ip = ipaddress.IPv4Address(url.hostname)
    endpoint = f"https://{ip}:7869/v1"
    if value.rstrip("/") != endpoint or not any(ip in network for network in LOBO_NETWORKS):
        raise ValueError("Invalid LOBO endpoint")
    return endpoint


def validate_pin(endpoint, pem):
    if not isinstance(pem, str) or len(pem) > 16384 or not PEM_PATTERN.fullmatch(pem):
        raise ValueError("Invalid LOBO certificate")
    SummaryProviderConfig(lobo_endpoint(endpoint), "lobo-nano", ca_pem=pem)
    return pem


def read_registry(value):
    if not isinstance(value, dict) or len(value) > MAX_TRUSTED_LOBO_DEVICES:
        raise ValueError("Invalid LOBO trust registry")
    result = {}
    for endpoint, pem in value.items():
        canonical = lobo_endpoint(endpoint)
        if canonical != endpoint:
            raise ValueError("Invalid LOBO trust endpoint")
        result[canonical] = validate_pin(canonical, pem)
    return result


def pinned_provider(config):
    if config is None or not config.ca_pem or config.api_key or config.allow_http or config.ca_file:
        return None
    try:
        endpoint = lobo_endpoint(config.base_url)
        return endpoint, validate_pin(endpoint, config.ca_pem)
    except (ValueError, TypeError):
        return None


def remember_provider(registry, config):
    result = dict(registry)
    entry = pinned_provider(config)
    if entry is not None:
        endpoint, pem = entry
        if endpoint not in result and len(result) >= MAX_TRUSTED_LOBO_DEVICES:
            raise ValueError("LOBO trust registry full")
        result[endpoint] = pem
    return result
