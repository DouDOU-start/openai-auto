from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from typing import Any

from curl_cffi import requests

from .settings import Settings


DEFAULT_SMSBOWER_API_BASE = "https://smsbower.page/stubs/handler_api.php"


class SmsBowerError(RuntimeError):
    """Base exception for SMSBower API failures."""


class SmsBowerApiError(SmsBowerError):
    """SMSBower returned an error response."""


class SmsBowerNoNumber(SmsBowerApiError):
    """No phone numbers are currently available for the requested filters."""


class SmsBowerTimeout(SmsBowerError):
    """Timed out while waiting for an SMS code."""


@dataclass(frozen=True)
class SmsBowerActivation:
    activation_id: str
    phone_number: str
    raw: str


@dataclass(frozen=True)
class SmsBowerCode:
    code: str
    raw: str


@dataclass(frozen=True)
class SmsBowerPriceOption:
    country: str
    service: str
    price: float
    count: int
    provider_id: str = ""


class SmsBowerClient:
    """Client for the SMSBower sms-activate-compatible API."""

    def __init__(self, settings: Settings):
        self._settings = settings

    def enabled(self) -> bool:
        return bool(self._settings.smsbower_api_key.strip())

    def get_balance(self) -> str:
        text = self._request_text({"action": "getBalance"})
        if not text.startswith("ACCESS_BALANCE:"):
            raise self._error_from_text(text)
        return text.split(":", 1)[1].strip()

    def get_number(self) -> SmsBowerActivation:
        params: dict[str, str] = {
            "action": "getNumber",
            "service": self._settings.smsbower_service,
        }
        self._add_optional(params, "country", self._settings.smsbower_country)
        self._add_optional(params, "maxPrice", self._settings.smsbower_max_price)
        self._add_optional(params, "minPrice", self._settings.smsbower_min_price)
        self._add_optional(params, "providerIds", self._settings.smsbower_provider_ids)
        self._add_optional(params, "exceptProviderIds", self._settings.smsbower_except_provider_ids)
        self._add_optional(params, "phoneException", self._settings.smsbower_phone_exception)

        text = self._request_text(params)
        if not text.startswith("ACCESS_NUMBER:"):
            raise self._error_from_text(text)
        parts = text.split(":", 2)
        if len(parts) != 3 or not parts[1].strip() or not parts[2].strip():
            raise SmsBowerApiError(f"SMSBower getNumber 返回格式异常: {text}")
        return SmsBowerActivation(
            activation_id=parts[1].strip(),
            phone_number=self.normalize_phone(parts[2]),
            raw=text,
        )

    def get_number_v2(self) -> dict[str, Any]:
        params: dict[str, str] = {
            "action": "getNumberV2",
            "service": self._settings.smsbower_service,
        }
        self._add_optional(params, "country", self._settings.smsbower_country)
        self._add_optional(params, "maxPrice", self._settings.smsbower_max_price)
        self._add_optional(params, "minPrice", self._settings.smsbower_min_price)
        self._add_optional(params, "providerIds", self._settings.smsbower_provider_ids)
        self._add_optional(params, "exceptProviderIds", self._settings.smsbower_except_provider_ids)
        text = self._request_text(params)
        data = self._json_from_text(text)
        if not isinstance(data, dict):
            raise SmsBowerApiError(f"SMSBower getNumberV2 返回格式异常: {text[:300]}")
        return data

    def get_status(self, activation_id: str) -> str:
        return self._request_text({"action": "getStatus", "id": str(activation_id).strip()})

    def wait_code(self, activation_id: str) -> SmsBowerCode:
        deadline = time.time() + max(5, int(self._settings.smsbower_timeout or 0))
        interval = max(1.0, float(self._settings.smsbower_poll_interval or 0))
        last_status = ""
        while time.time() < deadline:
            text = self.get_status(activation_id)
            last_status = text
            if text.startswith("STATUS_OK:"):
                code = self._extract_code(text.split(":", 1)[1])
                if not code:
                    raise SmsBowerApiError(f"SMSBower STATUS_OK 缺少验证码: {text}")
                return SmsBowerCode(code=code, raw=text)
            if text.startswith("STATUS_CANCEL"):
                raise SmsBowerApiError(f"SMSBower 激活已取消: {text}")
            if text.startswith("NO_ACTIVATION"):
                raise SmsBowerApiError(f"SMSBower 激活不存在: {text}")
            if self._is_fatal_error(text):
                raise self._error_from_text(text)
            time.sleep(interval)
        raise SmsBowerTimeout(f"等待 SMSBower 短信验证码超时: activation={activation_id}, last={last_status}")

    def mark_ready(self, activation_id: str) -> str:
        return self.set_status(activation_id, 1)

    def request_another_sms(self, activation_id: str) -> str:
        return self.set_status(activation_id, 3)

    def finish_activation(self, activation_id: str) -> str:
        return self.set_status(activation_id, 6)

    def cancel_activation(self, activation_id: str) -> str:
        return self.set_status(activation_id, 8)

    def set_status(self, activation_id: str, status: int) -> str:
        text = self._request_text(
            {
                "action": "setStatus",
                "id": str(activation_id).strip(),
                "status": str(int(status)),
            }
        )
        if text.startswith("ACCESS_"):
            return text
        raise self._error_from_text(text)

    def get_prices(self, *, service: str = "", country: str = "") -> dict[str, Any]:
        params = {"action": "getPrices"}
        self._add_optional(params, "service", service or self._settings.smsbower_service)
        self._add_optional(params, "country", country or self._settings.smsbower_country)
        data = self._json_from_text(self._request_text(params))
        if not isinstance(data, dict):
            raise SmsBowerApiError("SMSBower getPrices 未返回 JSON 对象")
        return data

    def get_prices_v2(self, *, service: str = "", country: str = "") -> dict[str, Any]:
        params = {"action": "getPricesV2"}
        self._add_optional(params, "service", service or self._settings.smsbower_service)
        self._add_optional(params, "country", country or self._settings.smsbower_country)
        data = self._json_from_text(self._request_text(params))
        if not isinstance(data, dict):
            raise SmsBowerApiError("SMSBower getPricesV2 未返回 JSON 对象")
        return data

    def get_prices_v3(self, *, service: str = "", country: str = "") -> dict[str, Any]:
        params = {"action": "getPricesV3"}
        self._add_optional(params, "service", service or self._settings.smsbower_service)
        self._add_optional(params, "country", country or self._settings.smsbower_country)
        data = self._json_from_text(self._request_text(params))
        if not isinstance(data, dict):
            raise SmsBowerApiError("SMSBower getPricesV3 未返回 JSON 对象")
        return data

    def get_services(self) -> Any:
        return self._json_from_text(self._request_text({"action": "getServicesList"}))

    def get_countries(self) -> Any:
        return self._json_from_text(self._request_text({"action": "getCountries"}))

    def get_top_countries_by_service(self, *, service: str = "") -> dict[str, Any]:
        params = {"action": "getTopCountriesByService", "service": service or self._settings.smsbower_service}
        data = self._json_from_text(self._request_text(params))
        if not isinstance(data, dict):
            raise SmsBowerApiError("SMSBower getTopCountriesByService 未返回 JSON 对象")
        return data

    def cheapest_prices(
        self,
        *,
        service: str = "",
        country: str = "",
        limit: int = 20,
        min_count: int = 1,
        version: int = 3,
    ) -> list[SmsBowerPriceOption]:
        service = service or self._settings.smsbower_service
        if version == 1:
            data = self.get_prices(service=service, country=country)
        elif version == 2:
            data = self.get_prices_v2(service=service, country=country)
        else:
            data = self.get_prices_v3(service=service, country=country)
        options = self.parse_price_options(data, service=service)
        filtered = [item for item in options if item.count >= min_count]
        filtered.sort(key=lambda item: (item.price, -item.count, item.country, item.provider_id))
        return filtered[: max(1, int(limit or 1))]

    def _request_text(self, params: dict[str, str]) -> str:
        if not self.enabled():
            raise SmsBowerError("SMSBower 未配置 api_key")
        request_params = {"api_key": self._settings.smsbower_api_key.strip(), **params}
        try:
            resp = requests.get(
                self._api_base(),
                params=request_params,
                timeout=max(1, int(self._settings.timeout or 30)),
                proxies=self._settings.smsbower_proxies,
                verify=self._settings.ssl_verify,
            )
        except Exception as exc:
            raise SmsBowerError(f"SMSBower 请求异常: {exc}") from exc
        text = str(resp.text or "").strip()
        if resp.status_code != 200:
            raise SmsBowerApiError(f"SMSBower HTTP {resp.status_code}: {text[:300]}")
        if not text:
            raise SmsBowerApiError("SMSBower 返回空响应")
        return text

    def _api_base(self) -> str:
        return (self._settings.smsbower_api_base or DEFAULT_SMSBOWER_API_BASE).strip() or DEFAULT_SMSBOWER_API_BASE

    @staticmethod
    def normalize_phone(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith("+"):
            return "+" + re.sub(r"\D", "", text)
        digits = re.sub(r"\D", "", text)
        return f"+{digits}" if digits else text

    @staticmethod
    def _add_optional(params: dict[str, str], key: str, value: object) -> None:
        text = str(value or "").strip()
        if text:
            params[key] = text

    @staticmethod
    def _extract_code(value: str) -> str:
        text = str(value or "").strip().strip("'\"")
        match = re.search(r"\d{4,8}", text)
        return match.group(0) if match else text

    @staticmethod
    def parse_price_options(data: dict[str, Any], *, service: str = "") -> list[SmsBowerPriceOption]:
        options: list[SmsBowerPriceOption] = []
        expected_service = str(service or "").strip()
        for country, service_map in data.items():
            if not isinstance(service_map, dict):
                continue
            for service_code, value in service_map.items():
                service_text = str(service_code or "").strip()
                if expected_service and service_text != expected_service:
                    continue
                options.extend(SmsBowerClient._price_options_from_value(str(country), service_text, value))
        return options

    @staticmethod
    def _price_options_from_value(country: str, service: str, value: Any) -> list[SmsBowerPriceOption]:
        if not isinstance(value, dict):
            return []
        if "cost" in value or "price" in value:
            price = SmsBowerClient._float_value(value.get("price", value.get("cost")))
            count = SmsBowerClient._int_value(value.get("count"))
            provider_id = str(value.get("provider_id") or value.get("providerId") or "").strip()
            return [SmsBowerPriceOption(country=country, service=service, price=price, count=count, provider_id=provider_id)]

        options: list[SmsBowerPriceOption] = []
        for key, nested in value.items():
            if isinstance(nested, dict) and ("price" in nested or "cost" in nested or "count" in nested):
                price = SmsBowerClient._float_value(nested.get("price", nested.get("cost", key)))
                count = SmsBowerClient._int_value(nested.get("count"))
                provider_id = str(nested.get("provider_id") or nested.get("providerId") or key or "").strip()
                options.append(
                    SmsBowerPriceOption(
                        country=country,
                        service=service,
                        price=price,
                        count=count,
                        provider_id=provider_id,
                    )
                )
                continue
            price = SmsBowerClient._float_value(key)
            count = SmsBowerClient._int_value(nested)
            options.append(SmsBowerPriceOption(country=country, service=service, price=price, count=count))
        return options

    @staticmethod
    def _json_from_text(text: str) -> Any:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SmsBowerApiError(f"SMSBower JSON 解析失败: {text[:300]}") from exc

    @staticmethod
    def _float_value(value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    @staticmethod
    def _int_value(value: Any) -> int:
        try:
            return int(value)
        except Exception:
            return 0

    @staticmethod
    def _is_fatal_error(text: str) -> bool:
        normalized = str(text or "").strip().upper()
        return normalized.startswith(("BAD_", "NO_ACTIVATION", "NO_BALANCE", "BANNED", "ERROR"))

    @staticmethod
    def _error_from_text(text: str) -> SmsBowerApiError:
        normalized = str(text or "").strip().upper()
        if normalized.startswith(("NO_NUMBERS", "NO_NUMBER")):
            return SmsBowerNoNumber(f"SMSBower 无可用号码: {text}")
        return SmsBowerApiError(f"SMSBower API 返回错误: {text}")
