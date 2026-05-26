from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .config import load_app_config
from .proxy_pool import pick_proxy_from_pool
from .settings import Settings, proxy_preview, resolve_proxy_pool
from .smsbower_client import SmsBowerClient


def main() -> None:
    args = build_parser().parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    cfg = load_app_config(Path(args.config).resolve())
    proxy_pool = resolve_proxy_pool(
        str(args.proxy or "").strip(),
        os.environ.get("PROTOCOL_REG_PROXIES", ""),
        os.environ.get("PROTOCOL_REG_PROXY", ""),
        getattr(cfg, "proxies", ()),
        getattr(cfg, "proxy", ""),
    )
    proxy = pick_proxy_from_pool(proxy_pool)
    if len(proxy_pool) > 1:
        print(f"[代理] 已配置 {len(proxy_pool)} 个代理，本次使用: {proxy_preview(proxy)}")
    elif proxy:
        print(f"[代理] 使用代理: {proxy_preview(proxy)}")

    settings = _settings_from_args(repo_root, cfg, args, proxy)
    client = SmsBowerClient(settings)
    if not client.enabled():
        raise SystemExit("[错误] 未配置 SMSBower key，可用 --key、SMSBOWER_API_KEY 或 config/protocol-reg.yaml 的 smsbower.key")

    try:
        _run_command(client, args)
    except Exception as exc:
        raise SystemExit(f"[错误] {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="SMSBower 独立工具")
    parser.add_argument(
        "--config",
        default=str(repo_root / "config" / "protocol-reg.yaml"),
        help="配置文件路径（YAML），默认 config/protocol-reg.yaml",
    )
    parser.add_argument("--proxy", default="", help="请求代理；多个代理可用逗号分隔")
    parser.add_argument("--api", default="", help="SMSBower API 地址，默认读取配置")
    parser.add_argument("--key", default="", help="SMSBower API key，默认读取配置或 SMSBOWER_API_KEY")
    parser.add_argument("--service", default="", help="服务代码，OpenAI (ChatGPT) 为 dr")
    parser.add_argument("--country", default="", help="国家代码；查低价时留空表示全量")
    parser.add_argument("--max-price", default="", help="取号最高价格")
    parser.add_argument("--min-price", default="", help="取号最低价格")
    parser.add_argument("--provider-ids", default="", help="只使用这些供应商 ID，英文逗号分隔")
    parser.add_argument("--except-provider-ids", default="", help="排除这些供应商 ID，英文逗号分隔")
    parser.add_argument("--phone-exception", default="", help="排除号码前缀，英文逗号分隔")
    parser.add_argument("--timeout", type=int, default=0, help="等待验证码超时秒数，默认配置值")
    parser.add_argument("--poll", type=float, default=0.0, help="等待验证码轮询间隔秒数，默认配置值")
    parser.add_argument("--no-proxy", action="store_true", help="SMSBower API 不走代理")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("balance", help="查询账户余额")
    subparsers.add_parser("services", help="输出服务列表 JSON")
    subparsers.add_parser("countries", help="输出国家列表 JSON")

    prices = subparsers.add_parser("prices", help="输出价格表 JSON")
    prices.add_argument("--version", type=int, choices=(1, 2, 3), default=3, help="价格接口版本，默认 v3")
    _add_filter_args(prices)

    cheap = subparsers.add_parser("cheap", help="按价格排序列出便宜国家/供应商")
    cheap.add_argument("--version", type=int, choices=(1, 2, 3), default=3, help="价格接口版本，默认 v3")
    cheap.add_argument("--limit", type=int, default=20, help="输出条数")
    cheap.add_argument("--min-count", type=int, default=1, help="最低可用数量")
    _add_filter_args(cheap)

    top = subparsers.add_parser("top-countries", help="输出指定服务的推荐国家 JSON")
    top.add_argument("--service", default=argparse.SUPPRESS, help="覆盖全局服务代码")

    get_number = subparsers.add_parser("get-number", help="获取一个号码")
    get_number.add_argument("--v2", action="store_true", help="使用 getNumberV2，返回完整 JSON")
    _add_filter_args(get_number, include_price=True, include_provider=True)

    status = subparsers.add_parser("status", help="查询激活状态")
    status.add_argument("activation_id", help="激活 ID")

    wait_code = subparsers.add_parser("wait-code", help="等待并输出验证码")
    wait_code.add_argument("activation_id", help="激活 ID")

    set_status = subparsers.add_parser("set-status", help="修改激活状态")
    set_status.add_argument("activation_id", help="激活 ID")
    set_status.add_argument("status", type=int, choices=(1, 3, 6, 8), help="1 就绪，3 下一条短信，6 完成，8 取消")

    return parser


def _add_filter_args(
    parser: argparse.ArgumentParser,
    *,
    include_price: bool = False,
    include_provider: bool = False,
) -> None:
    parser.add_argument("--service", default=argparse.SUPPRESS, help="覆盖全局服务代码")
    parser.add_argument("--country", default=argparse.SUPPRESS, help="覆盖全局国家代码")
    if include_price:
        parser.add_argument("--max-price", default=argparse.SUPPRESS, help="覆盖全局最高价格")
        parser.add_argument("--min-price", default=argparse.SUPPRESS, help="覆盖全局最低价格")
    if include_provider:
        parser.add_argument("--provider-ids", default=argparse.SUPPRESS, help="覆盖全局供应商 ID")
        parser.add_argument("--except-provider-ids", default=argparse.SUPPRESS, help="覆盖全局排除供应商 ID")
        parser.add_argument("--phone-exception", default=argparse.SUPPRESS, help="覆盖全局排除号码前缀")


def _run_command(client: SmsBowerClient, args: argparse.Namespace) -> None:
    command = str(args.command or "")
    if command == "balance":
        print(client.get_balance())
        return
    if command == "services":
        _print_json(client.get_services())
        return
    if command == "countries":
        _print_json(client.get_countries())
        return
    if command == "prices":
        _print_json(_prices(client, args))
        return
    if command == "cheap":
        for item in client.cheapest_prices(
            service=args.service,
            country=args.country,
            version=args.version,
            limit=args.limit,
            min_count=args.min_count,
        ):
            provider = f"\tprovider={item.provider_id}" if item.provider_id else ""
            print(f"{item.country}\tservice={item.service}\tprice={item.price:g}\tcount={item.count}{provider}")
        return
    if command == "top-countries":
        _print_json(client.get_top_countries_by_service(service=args.service))
        return
    if command == "get-number":
        if args.v2:
            _print_json(client.get_number_v2())
            return
        activation = client.get_number()
        print(f"activation_id={activation.activation_id}")
        print(f"phone={activation.phone_number}")
        print(f"raw={activation.raw}")
        return
    if command == "status":
        print(client.get_status(args.activation_id))
        return
    if command == "wait-code":
        result = client.wait_code(args.activation_id)
        print(f"code={result.code}")
        print(f"raw={result.raw}")
        return
    if command == "set-status":
        print(client.set_status(args.activation_id, args.status))
        return
    raise RuntimeError(f"未知命令: {command}")


def _prices(client: SmsBowerClient, args: argparse.Namespace) -> dict[str, Any]:
    if args.version == 1:
        return client.get_prices(service=args.service, country=args.country)
    if args.version == 2:
        return client.get_prices_v2(service=args.service, country=args.country)
    return client.get_prices_v3(service=args.service, country=args.country)


def _settings_from_args(repo_root: Path, cfg: Any, args: argparse.Namespace, proxy: str) -> Settings:
    smsbower_api = str(args.api or "").strip() or os.environ.get("SMSBOWER_API", "").strip() or cfg.smsbower_api
    smsbower_key = (
        str(args.key or "").strip()
        or os.environ.get("SMSBOWER_API_KEY", "").strip()
        or os.environ.get("SMSBOWER_KEY", "").strip()
        or cfg.smsbower_key
    )
    smsbower_service = _arg_text(args, "service") or os.environ.get("SMSBOWER_SERVICE", "").strip() or cfg.smsbower_service or "dr"
    smsbower_country = _arg_text(args, "country") or os.environ.get("SMSBOWER_COUNTRY", "").strip() or cfg.smsbower_country
    smsbower_timeout = int(args.timeout) if int(args.timeout or 0) > 0 else int(os.environ.get("SMSBOWER_TIMEOUT", "0") or 0) or cfg.smsbower_timeout
    smsbower_poll = float(args.poll) if float(args.poll or 0) > 0 else float(os.environ.get("SMSBOWER_POLL", "0") or 0) or cfg.smsbower_poll
    use_proxy = False if args.no_proxy else _boolish(os.environ.get("SMSBOWER_USE_PROXY"), cfg.use_proxy_for_smsbower)
    return Settings(
        project_root=repo_root.resolve(),
        proxy=str(proxy or "").strip(),
        license_file=None,
        login_delay=0,
        timeout=30,
        ssl_verify=True,
        email_code_api_base="",
        email_code_api_key="",
        email_code_sender_suffix="openai.com",
        email_code_poll_interval=2.0,
        email_code_timeout=120,
        smsbower_api_base=str(smsbower_api or "").strip(),
        smsbower_api_key=str(smsbower_key or "").strip(),
        smsbower_service=str(smsbower_service or "dr").strip() or "dr",
        smsbower_country=smsbower_country,
        smsbower_max_price=str(_arg_text(args, "max_price") or os.environ.get("SMSBOWER_MAX_PRICE", "") or cfg.smsbower_max_price).strip(),
        smsbower_min_price=str(_arg_text(args, "min_price") or os.environ.get("SMSBOWER_MIN_PRICE", "") or cfg.smsbower_min_price).strip(),
        smsbower_provider_ids=str(_arg_text(args, "provider_ids") or os.environ.get("SMSBOWER_PROVIDER_IDS", "") or cfg.smsbower_provider_ids).strip(),
        smsbower_except_provider_ids=str(
            _arg_text(args, "except_provider_ids") or os.environ.get("SMSBOWER_EXCEPT_PROVIDER_IDS", "") or cfg.smsbower_except_provider_ids
        ).strip(),
        smsbower_phone_exception=str(
            _arg_text(args, "phone_exception") or os.environ.get("SMSBOWER_PHONE_EXCEPTION", "") or cfg.smsbower_phone_exception
        ).strip(),
        smsbower_timeout=max(5, int(smsbower_timeout)),
        smsbower_poll_interval=max(1.0, float(smsbower_poll)),
        use_proxy_for_smsbower=bool(use_proxy),
    )


def _arg_text(args: argparse.Namespace, name: str) -> str:
    return str(getattr(args, name, "") or "").strip()


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _boolish(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


if __name__ == "__main__":
    main()
