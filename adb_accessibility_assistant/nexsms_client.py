"""NexSMS.net API client for phone number verification services."""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime
from typing import Any, Callable

import requests

from .phone_activation_store import PhoneActivationStore


PHONE_CODE_TIMEOUT_ERROR_PREFIX = "PHONE_CODE_TIMEOUT::"
PHONE_NUMBER_REJECTED_ERROR_PREFIX = "PHONE_NUMBER_REJECTED::"
DEFAULT_ACTIVATION_VALIDITY_SECONDS = 20 * 60

_NO_NUMBERS_PATTERN = re.compile(
    r"numbers?\s+not\s+found|no\s+numbers|no\s+stock|not\s+available|库存.*0|暂无可用",
    re.IGNORECASE,
)
_PENDING_SMS_PATTERN = re.compile(
    r"no\s+sms|waiting|not\s+arrived|empty|no\s+records|未收到|暂无短信|短信为空",
    re.IGNORECASE,
)
_TERMINAL_ERROR_PATTERN = re.compile(
    r"invalid\s*api\s*key|bad[_\s-]*key|wrong[_\s-]*key|unauthorized|forbidden|"
    r"no\s*balance|insufficient\s*balance|余额不足|账号.*封禁|banned",
    re.IGNORECASE,
)
_INDONESIA_ALIASES = {"indonesia", "印尼", "印度尼西亚", "id"}


class NexSMSError(RuntimeError):
    """Raised when a NexSMS API operation fails."""

    def __init__(
        self,
        message: str,
        *,
        payload: Any | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.payload = payload
        self.status = status


def describe_payload(raw: Any) -> str:
    """Convert a NexSMS payload into a readable status string."""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        for key in ("message", "error", "msg", "statusText"):
            value = str(raw.get(key) or "").strip()
            if value:
                return value
        try:
            return json.dumps(raw, ensure_ascii=False)
        except TypeError:
            return str(raw)
    if raw is None:
        return ""
    return str(raw).strip()


def is_success_payload(payload: Any) -> bool:
    """Return True when the payload is a standard NexSMS success envelope."""
    return isinstance(payload, dict) and not isinstance(payload, list) and payload.get("code") == 0


def is_no_numbers_error(payload_or_message: Any) -> bool:
    """Return True when the response indicates stock exhaustion."""
    return bool(_NO_NUMBERS_PATTERN.search(describe_payload(payload_or_message)))


def is_pending_message(payload_or_message: Any) -> bool:
    """Return True when the response indicates SMS is not available yet."""
    text = describe_payload(payload_or_message)
    return not text or bool(_PENDING_SMS_PATTERN.search(text))


def is_terminal_error(payload_or_message: Any, status: int | None = None) -> bool:
    """Return True when the error should abort instead of being retried."""
    if status in (401, 403):
        return True
    return bool(_TERMINAL_ERROR_PATTERN.search(describe_payload(payload_or_message)))


def extract_verification_code(raw_code_or_text: Any) -> str:
    """Extract a 4-8 digit verification code from arbitrary SMS text."""
    text = str(raw_code_or_text or "").strip()
    if not text:
        return ""
    digit_match = re.search(r"\b(\d{4,8})\b", text)
    return digit_match.group(1) if digit_match else ""


def is_phone_code_timeout_error(message: str) -> bool:
    """Return True when the error message is an OTP polling timeout."""
    return str(message or "").startswith(PHONE_CODE_TIMEOUT_ERROR_PREFIX)


def is_phone_number_rejected_error(message: str) -> bool:
    """Return True when GoPay rejected the submitted phone number / login path."""
    return str(message or "").startswith(PHONE_NUMBER_REJECTED_ERROR_PREFIX)


def _is_indonesia_alias(value: Any) -> bool:
    normalized = str(value or "").strip().casefold()
    return normalized in _INDONESIA_ALIASES


def _normalize_price(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric <= 0:
        return None
    return round(numeric, 4)


def _parse_epoch_like(value: Any, *, now: float) -> float | None:
    if value in (None, ""):
        return None

    if isinstance(value, (int, float)):
        numeric = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            numeric = float(text)
        except ValueError:
            normalized = text.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(normalized).timestamp()
            except ValueError:
                return None

    if not math.isfinite(numeric) or numeric <= 0:
        return None
    if numeric > 1_000_000_000_000:
        return numeric / 1000.0
    if numeric > 1_000_000_000:
        return numeric
    return now + numeric


def _build_sorted_unique_prices(values: list[Any]) -> list[float]:
    unique: set[float] = set()
    for value in values:
        normalized = _normalize_price(value)
        if normalized is not None:
            unique.add(normalized)
    return sorted(unique)


def _country_id_from_entry(country: dict[str, Any]) -> int | None:
    raw = country.get("countryId", country.get("id", country.get("country")))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _country_name_from_entry(country: dict[str, Any]) -> str:
    for key in (
        "countryName",
        "name",
        "countryEnName",
        "enName",
        "englishName",
        "countryCnName",
        "cnName",
        "zhName",
        "title",
        "label",
    ):
        name = str(country.get(key, "")).strip()
        if name:
            return name
    country_id = _country_id_from_entry(country)
    if country_id is not None:
        return f"Country #{country_id}"
    return "Unknown country"


def _country_name_candidates(country: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in (
        "countryName",
        "name",
        "countryEnName",
        "enName",
        "englishName",
        "countryCnName",
        "cnName",
        "zhName",
        "title",
        "label",
    ):
        value = str(country.get(key, "")).strip()
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def find_country_entry(countries: list[dict[str, Any]], name_or_id: str) -> dict[str, Any] | None:
    """Find a country entry by country name or numeric ID."""
    query = str(name_or_id or "").strip()
    if not query:
        return None

    try:
        target_id = int(query)
    except ValueError:
        target_id = None

    if target_id is not None:
        for country in countries:
            country_id = _country_id_from_entry(country)
            if country_id == target_id:
                return country

    query_lower = query.casefold()
    for country in countries:
        for country_name in _country_name_candidates(country):
            country_name_lower = country_name.casefold()
            if query_lower in country_name_lower or country_name_lower in query_lower:
                return country

    if _is_indonesia_alias(query_lower):
        for country in countries:
            if _country_id_from_entry(country) == 6:
                return country
    return None


def find_country_id(countries: list[dict], name: str) -> str | None:
    """Find country ID by name (case-insensitive partial match)."""
    match = find_country_entry(countries, name)
    if not match:
        return None
    country_id = _country_id_from_entry(match)
    return str(country_id) if country_id is not None else None


def find_service_code(services: list[dict], name: str) -> str | None:
    """Find service code by name (case-insensitive partial match)."""
    name_lower = name.casefold()
    for service in services:
        service_name = str(service.get("serviceName", "")).casefold()
        service_code = str(service.get("serviceCode", "")).casefold()
        if name_lower in service_name or name_lower in service_code:
            return str(service.get("serviceCode"))
    return None


class NexSMSClient:
    """Client for nexsms.net API to purchase phone numbers and retrieve SMS codes."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.nexsms.net",
        proxy: str = "",
        activation_db_path: str = "artifacts/nexsms_activations.sqlite3",
        reuse_existing_number_min_remaining_minutes: float = 15.0,
        activation_validity_minutes: float = 20.0,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.proxy = proxy.strip()
        self.timeout = timeout
        self.reuse_existing_number_min_remaining_minutes = max(
            0.0,
            float(reuse_existing_number_min_remaining_minutes or 0.0),
        )
        self.activation_validity_seconds = max(
            60.0,
            float(activation_validity_minutes or (DEFAULT_ACTIVATION_VALIDITY_SECONDS / 60.0)) * 60.0,
        )
        self._session = requests.Session()
        self._session.trust_env = False
        self._activation_store = PhoneActivationStore(activation_db_path) if activation_db_path else None
        if self.proxy:
            self._session.proxies.update(
                {
                    "http": self.proxy,
                    "https": self.proxy,
                }
            )

    def _parse_response_payload(self, response: requests.Response) -> Any:
        text = response.text.strip()
        if not text:
            return {}
        try:
            return response.json()
        except ValueError:
            return text

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        *,
        allow_api_error: bool = False,
    ) -> Any:
        """Make an API request with robust payload parsing and error handling."""
        url = f"{self.base_url}{endpoint}"
        request_params = dict(params or {})
        request_params["apiKey"] = self.api_key

        try:
            response = self._session.request(
                method=method,
                url=url,
                params=request_params,
                json=json_data,
                timeout=self.timeout,
            )
            payload = self._parse_response_payload(response)
            if not response.ok:
                raise NexSMSError(
                    f"API request failed ({response.status_code}): {describe_payload(payload) or response.reason}",
                    payload=payload,
                    status=response.status_code,
                )
            if not allow_api_error and isinstance(payload, dict) and payload.get("code") != 0:
                raise NexSMSError(
                    f"API error: {describe_payload(payload) or 'Unknown error'}",
                    payload=payload,
                    status=response.status_code,
                )
            return payload
        except requests.exceptions.RequestException as exc:
            raise NexSMSError(f"API request failed: {exc}") from exc

    def get_countries(self) -> list[dict[str, Any]]:
        """Get list of supported countries."""
        data = self._request("GET", "/api/countries")
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        if isinstance(data, list):
            return data
        raise NexSMSError(f"Unexpected response format for countries: {data}")

    def get_services(self) -> list[dict[str, Any]]:
        """Get list of available services."""
        data = self._request("GET", "/api/services")
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        if isinstance(data, list):
            return data
        raise NexSMSError(f"Unexpected response format for services: {data}")

    def get_price(self, service_code: str, country_id: str) -> dict[str, Any]:
        """Query price data for a service in a specific country."""
        endpoints = (
            "/api/getCountryByService",
            "/api/price",
        )
        for endpoint in endpoints:
            try:
                data = self._request(
                    "GET",
                    endpoint,
                    params={"serviceCode": service_code, "countryId": country_id},
                )
            except NexSMSError:
                continue

            if isinstance(data, dict) and "data" in data:
                inner = data["data"]
                if isinstance(inner, dict):
                    return {
                        "minPrice": inner.get("minPrice"),
                        "medianPrice": inner.get("medianPrice"),
                        "maxPrice": inner.get("maxPrice"),
                        "priceMap": inner.get("priceMap") or {},
                        "countryName": inner.get("countryName"),
                    }
            if isinstance(data, dict):
                return data

        return {
            "minPrice": None,
            "medianPrice": None,
            "maxPrice": None,
            "priceMap": {},
            "note": "Price API unavailable - use configured default_price",
        }

    def place_order(
        self,
        service_code: str,
        country_id: str | int,
        price: float,
        quantity: int = 1,
    ) -> dict[str, Any]:
        """Place an order to get phone numbers."""
        data = self._request(
            "POST",
            "/api/order/purchase",
            json_data={
                "serviceCode": service_code,
                "countryId": int(country_id),
                "quantity": quantity,
                "price": price,
            },
        )
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        if isinstance(data, dict):
            return data
        raise NexSMSError(f"Unexpected response format for purchase: {data}")

    def _collect_price_candidates(
        self,
        price_data: dict[str, Any],
        *,
        default_price: float | None,
    ) -> list[float]:
        candidates: list[Any] = [
            price_data.get("minPrice"),
            price_data.get("medianPrice"),
            price_data.get("maxPrice"),
        ]
        price_map = price_data.get("priceMap")
        if isinstance(price_map, dict):
            for price_key, available_count in price_map.items():
                try:
                    count = int(available_count)
                except (TypeError, ValueError):
                    count = 0
                if count > 0:
                    candidates.append(price_key)
        normalized_candidates = _build_sorted_unique_prices(candidates)
        if normalized_candidates:
            return normalized_candidates
        if default_price is None:
            return []
        fallback_price = _normalize_price(default_price)
        return [fallback_price] if fallback_price is not None else []

    def _order_prices(
        self,
        prices: list[float],
        *,
        acquire_priority: str,
        preferred_price: float | None,
    ) -> list[float]:
        ordered = sorted(prices, reverse=acquire_priority == "price_high")
        preferred = _normalize_price(preferred_price)
        if preferred is None:
            return ordered
        return [preferred, *[price for price in ordered if price != preferred]]

    def _filter_prices_in_range(
        self,
        prices: list[float],
        *,
        min_price: float | None,
        max_price: float | None,
    ) -> list[float]:
        floor = _normalize_price(min_price)
        ceiling = _normalize_price(max_price)
        filtered: list[float] = []
        for price in prices:
            if floor is not None and price < floor:
                continue
            if ceiling is not None and price > ceiling:
                continue
            filtered.append(price)
        return filtered

    def _extract_phone_number_from_order(self, payload: dict[str, Any]) -> str | None:
        candidates: list[Any] = [
            payload.get("phoneNumber"),
            payload.get("phone"),
        ]
        phone_numbers = payload.get("phoneNumbers")
        if isinstance(phone_numbers, list):
            candidates.extend(phone_numbers)
        numbers = payload.get("numbers")
        if isinstance(numbers, list):
            candidates.extend(numbers)

        for candidate in candidates:
            phone = str(candidate or "").strip()
            if phone:
                return phone
        return None

    def _extract_expiry_epoch_from_order(self, payload: dict[str, Any], *, now: float) -> float | None:
        direct_keys = (
            "expiredAt",
            "expireAt",
            "expiresAt",
            "expiryAt",
            "expireTime",
            "expiryTime",
            "expiredTime",
            "validUntil",
            "deadline",
            "expireDate",
            "ttlSeconds",
            "ttl",
            "expireSeconds",
            "validSeconds",
            "remainingSeconds",
            "leftTime",
        )
        for key in direct_keys:
            parsed = _parse_epoch_like(payload.get(key), now=now)
            if parsed is not None:
                return parsed

        for nested_key in ("meta", "extra", "info"):
            nested = payload.get(nested_key)
            if not isinstance(nested, dict):
                continue
            for key in direct_keys:
                parsed = _parse_epoch_like(nested.get(key), now=now)
                if parsed is not None:
                    return parsed
        return None

    def _effective_expiry_epoch(self, *, acquired_at_epoch: float, provider_expiry_epoch: float | None) -> tuple[float, str]:
        capped_local_expiry = acquired_at_epoch + self.activation_validity_seconds
        validity_minutes_label = int(round(self.activation_validity_seconds / 60.0))
        if provider_expiry_epoch is None:
            return capped_local_expiry, f"assumed_default_{validity_minutes_label}m"
        if provider_expiry_epoch > capped_local_expiry:
            return capped_local_expiry, f"provider_capped_{validity_minutes_label}m"
        return provider_expiry_epoch, "provider"

    def _requested_country_entries(self, *, country_name: str, country_order: list[str] | None) -> list[str]:
        requested = [str(entry).strip() for entry in (country_order or []) if str(entry).strip()]
        if not requested and str(country_name).strip():
            requested = [str(country_name).strip()]
        return requested

    def _stored_activation_matches_country(self, activation: dict[str, Any], requested: list[str]) -> bool:
        if not requested:
            return True

        country_name = str(activation.get("country_name") or "").strip()
        country_name_lower = country_name.casefold()
        try:
            country_id = int(activation.get("country_id"))
        except (TypeError, ValueError):
            country_id = None

        for entry in requested:
            query = str(entry or "").strip()
            if not query:
                continue
            try:
                if country_id is not None and country_id == int(query):
                    return True
            except ValueError:
                pass

            query_lower = query.casefold()
            if query_lower and (
                query_lower in country_name_lower or country_name_lower in query_lower
            ):
                return True
            if _is_indonesia_alias(query_lower) and (
                country_id == 6 or _is_indonesia_alias(country_name_lower)
            ):
                return True
        return False

    def _find_reusable_local_activation(
        self,
        *,
        service_code: str,
        country_name: str,
        country_order: list[str] | None,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any] | None:
        if self._activation_store is None:
            return None

        log = log_callback or (lambda _message: None)
        requested_countries = self._requested_country_entries(
            country_name=country_name,
            country_order=country_order,
        )
        min_remaining_seconds = self.reuse_existing_number_min_remaining_minutes * 60.0
        candidates = self._activation_store.list_reusable_candidates(
            service_code=service_code,
            min_remaining_seconds=min_remaining_seconds,
        )
        now = time.time()
        for candidate in candidates:
            if not self._stored_activation_matches_country(candidate, requested_countries):
                continue
            remaining_minutes = max(0.0, (float(candidate["expiry_epoch"]) - now) / 60.0)
            log(
                "Reusing cached NexSMS number: "
                f"{candidate['phone_number']} (remaining {remaining_minutes:.1f}m)"
            )
            return {
                "phone_number": str(candidate["phone_number"]),
                "country_id": candidate.get("country_id"),
                "country_name": str(candidate.get("country_name") or country_name or "Unknown country"),
                "price": candidate.get("price"),
                "acquired_at_epoch": float(candidate.get("acquired_at_epoch") or now),
                "expiry_epoch": float(candidate.get("expiry_epoch") or 0.0),
                "expiry_source": "db_reuse",
                "order_result": {"source": "db_reuse"},
                "reused_existing": True,
            }
        return None

    def mark_number_invalid(self, phone_number: str, *, reason: str = "") -> None:
        if self._activation_store is None:
            return
        self._activation_store.mark_invalid(phone_number, reason=reason)

    def mark_number_consumed(self, phone_number: str, *, reason: str = "") -> None:
        if self._activation_store is None:
            return
        self._activation_store.mark_consumed(phone_number, reason=reason)

    def _resolve_country_candidates(
        self,
        countries: list[dict[str, Any]],
        *,
        country_name: str,
        country_order: list[str] | None,
    ) -> list[dict[str, Any]]:
        requested = [entry for entry in (country_order or []) if str(entry).strip()]
        if not requested and str(country_name).strip():
            requested = [country_name]

        resolved: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for request in requested:
            country = find_country_entry(countries, str(request))
            if not country:
                continue
            country_id = _country_id_from_entry(country)
            if country_id is None or country_id in seen_ids:
                continue
            resolved.append(country)
            seen_ids.add(country_id)

        if resolved:
            return resolved

        fallback = find_country_entry(countries, country_name)
        if fallback:
            return [fallback]
        if _is_indonesia_alias(country_name):
            return [{"countryId": 6, "countryName": "Indonesia"}]
        sample_labels = ", ".join(_country_name_from_entry(country) for country in countries[:8])
        raise NexSMSError(
            f"Country not found: {country_name or requested}. Available sample: {sample_labels}"
        )

    def acquire_number(
        self,
        *,
        service_code: str,
        country_name: str,
        country_order: list[str] | None = None,
        default_price: float | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        preferred_price: float | None = None,
        acquire_priority: str = "country",
        retry_rounds: int = 3,
        retry_delay_ms: int = 2000,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Acquire a phone number with country/price ranking and retry strategy."""
        log = log_callback or (lambda _message: None)
        reusable = self._find_reusable_local_activation(
            service_code=service_code,
            country_name=country_name,
            country_order=country_order,
            log_callback=log_callback,
        )
        if reusable is not None:
            return reusable

        priority = str(acquire_priority or "country").strip().lower()
        if priority not in {"country", "price", "price_high"}:
            priority = "country"

        rounds = max(1, min(10, int(retry_rounds or 1)))
        delay_seconds = max(0.5, float(retry_delay_ms or 0) / 1000.0)
        normalized_min_price = _normalize_price(min_price)
        normalized_max_price = _normalize_price(max_price)
        if (
            normalized_min_price is not None
            and normalized_max_price is not None
            and normalized_min_price > normalized_max_price
        ):
            raise NexSMSError(
                f"Invalid NexSMS price range: min_price={normalized_min_price} > max_price={normalized_max_price}"
            )

        try:
            countries = self.get_countries()
            country_candidates = self._resolve_country_candidates(
                countries,
                country_name=country_name,
                country_order=country_order,
            )
        except NexSMSError:
            if country_order:
                raise
            if not _is_indonesia_alias(country_name):
                raise
            log("NexSMS countries lookup unavailable; falling back to hardcoded Indonesia countryId=6")
            country_candidates = [{"countryId": 6, "countryName": "Indonesia"}]

        last_error: NexSMSError | None = None
        final_no_numbers: list[str] = []

        for round_index in range(1, rounds + 1):
            country_plans: list[dict[str, Any]] = []
            no_numbers_reasons: list[str] = []

            for country in country_candidates:
                country_id = _country_id_from_entry(country)
                if country_id is None:
                    continue
                label = _country_name_from_entry(country)
                price_data = self.get_price(service_code, str(country_id))
                all_prices = self._collect_price_candidates(price_data, default_price=default_price)
                ordered_prices = self._order_prices(
                    all_prices,
                    acquire_priority=priority,
                    preferred_price=preferred_price,
                )
                candidate_prices = self._filter_prices_in_range(
                    ordered_prices,
                    min_price=normalized_min_price,
                    max_price=normalized_max_price,
                )
                country_plans.append(
                    {
                        "country_id": country_id,
                        "country_name": label,
                        "prices": candidate_prices,
                        "raw_prices": ordered_prices,
                        "price_data": price_data,
                    }
                )

            if priority in {"price", "price_high"}:
                country_plans.sort(
                    key=lambda plan: (
                        plan["prices"][0] if plan["prices"] else math.inf,
                        plan["country_name"],
                    ),
                    reverse=priority == "price_high",
                )
                ranking_summary = " | ".join(
                    f"{plan['country_name']}:{plan['prices'][0] if plan['prices'] else 'none'}"
                    for plan in country_plans
                )
                if ranking_summary:
                    log(f"NexSMS price-priority ranking: {ranking_summary}")

            for plan in country_plans:
                country_id = plan["country_id"]
                country_label = plan["country_name"]
                prices = plan["prices"]
                raw_prices = plan["raw_prices"]

                if not prices:
                    if raw_prices:
                        no_numbers_reasons.append(
                            f"{country_label}: no usable price in range "
                            f"[{normalized_min_price if normalized_min_price is not None else '-inf'}, "
                            f"{normalized_max_price if normalized_max_price is not None else '+inf'}]"
                        )
                    else:
                        no_numbers_reasons.append(f"{country_label}: no available price candidates")
                    continue

                for price in prices:
                    log(
                        "Ordering NexSMS number: "
                        f"service={service_code}, country={country_label} ({country_id}), price={price}"
                    )
                    try:
                        order_result = self.place_order(service_code, country_id, price)
                    except NexSMSError as exc:
                        if is_terminal_error(exc.payload or str(exc), exc.status):
                            raise
                        if is_no_numbers_error(exc.payload or str(exc)):
                            continue
                        last_error = exc
                        continue

                    phone_number = self._extract_phone_number_from_order(order_result)
                    if not phone_number:
                        last_error = NexSMSError("NexSMS purchase succeeded, but no phone number was returned.")
                        continue
                    acquired_at_epoch = time.time()
                    provider_expiry_epoch = self._extract_expiry_epoch_from_order(
                        order_result,
                        now=acquired_at_epoch,
                    )
                    expiry_epoch, expiry_source = self._effective_expiry_epoch(
                        acquired_at_epoch=acquired_at_epoch,
                        provider_expiry_epoch=provider_expiry_epoch,
                    )
                    if self._activation_store is not None:
                        self._activation_store.save_purchase(
                            phone_number=phone_number,
                            service_code=service_code,
                            country_name=country_label,
                            country_id=country_id,
                            price=price,
                            acquired_at_epoch=acquired_at_epoch,
                            expiry_epoch=expiry_epoch,
                        )

                    return {
                        "phone_number": phone_number,
                        "country_id": country_id,
                        "country_name": country_label,
                        "price": price,
                        "acquired_at_epoch": acquired_at_epoch,
                        "expiry_epoch": expiry_epoch,
                        "expiry_source": expiry_source,
                        "order_result": order_result,
                    }

                no_numbers_reasons.append(
                    f"{country_label}: exhausted price candidates {', '.join(str(price) for price in prices)}"
                )

            final_no_numbers = no_numbers_reasons
            if no_numbers_reasons and round_index < rounds:
                log(
                    f"NexSMS no numbers available in round {round_index}/{rounds}; "
                    f"retrying after {delay_seconds:.1f}s"
                )
                time.sleep(delay_seconds)
                continue
            break

        if final_no_numbers:
            raise NexSMSError(
                "NexSMS could not acquire a number: " + " | ".join(final_no_numbers)
            )
        if last_error is not None:
            raise last_error
        raise NexSMSError("NexSMS failed to acquire phone number.")

    def _extract_code_from_payload(self, payload: Any) -> str | None:
        candidates: list[Any] = []

        if isinstance(payload, str):
            code = extract_verification_code(payload)
            return code or None

        if not isinstance(payload, dict):
            return None

        data = payload.get("data")
        if isinstance(data, dict):
            candidates.extend(
                [
                    data.get("code"),
                    data.get("text"),
                    data.get("sms"),
                    data.get("message"),
                ]
            )
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    candidates.extend(
                        [
                            item.get("code"),
                            item.get("text"),
                            item.get("sms"),
                            item.get("message"),
                        ]
                    )
                else:
                    candidates.append(item)
        else:
            candidates.append(data)

        candidates.extend(
            [
                payload.get("message"),
                payload.get("msg"),
                payload.get("error"),
            ]
        )

        for candidate in candidates:
            code = extract_verification_code(candidate)
            if code:
                return code
        return None

    def get_sms_status(self, phone_number: str, format: str = "json_latest") -> tuple[str | None, str]:
        """Poll SMS state once and return both code and normalized status text."""
        payload = self._request(
            "GET",
            "/api/sms/messages",
            params={"phoneNumber": phone_number, "format": format},
            allow_api_error=True,
        )

        code = self._extract_code_from_payload(payload)
        status_text = describe_payload(payload) or "PENDING"
        if code:
            return code, status_text

        if isinstance(payload, dict) and not is_success_payload(payload):
            if is_terminal_error(payload):
                raise NexSMSError(
                    f"NexSMS get sms messages failed: {status_text}",
                    payload=payload,
                )
            if is_pending_message(payload):
                return None, status_text

        if isinstance(payload, str) and is_pending_message(payload):
            return None, status_text

        if payload in ({}, "", None):
            return None, status_text

        return None, status_text

    def get_sms(self, phone_number: str, format: str = "json_latest") -> str | None:
        """Poll for an SMS verification code once."""
        code, _ = self.get_sms_status(phone_number, format=format)
        return code

    def wait_for_sms(
        self,
        phone_number: str,
        poll_interval: float = 5.0,
        timeout: float = 120.0,
        *,
        ignore_codes: set[str] | None = None,
        baseline_status_text: str | None = None,
    ) -> str:
        """Poll for SMS code with timeout and better timeout diagnostics."""
        start_time = time.monotonic()
        last_status = ""
        normalized_ignore_codes = {
            str(code).strip()
            for code in (ignore_codes or set())
            if str(code).strip()
        }
        baseline = str(baseline_status_text or "").strip()
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                suffix = f" Last status: {last_status}" if last_status else ""
                raise NexSMSError(
                    f"{PHONE_CODE_TIMEOUT_ERROR_PREFIX}SMS code not received within {timeout}s for {phone_number}.{suffix}"
                )

            code, last_status = self.get_sms_status(phone_number, format="json_latest")
            if code:
                if code in normalized_ignore_codes:
                    time.sleep(max(1.0, poll_interval))
                    continue
                return code

            if baseline and last_status == baseline:
                time.sleep(max(1.0, poll_interval))
                continue

            time.sleep(max(1.0, poll_interval))

    def close_activation(self, phone_number: str) -> str:
        """Close a purchased activation / phone number."""
        payload = self._request(
            "POST",
            "/api/close/activation",
            json_data={"phoneNumber": phone_number},
            allow_api_error=True,
        )
        if isinstance(payload, dict) and payload.get("code") != 0:
            raise NexSMSError(
                f"NexSMS close activation failed: {describe_payload(payload) or 'Unknown error'}",
                payload=payload,
            )
        self.mark_number_invalid(phone_number, reason="close_activation")
        return describe_payload(payload)
