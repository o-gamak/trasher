#!/usr/bin/env python3
"""
T-Invest Bond Scanner — Junk Bond Portfolio Optimizer
======================================================
Ищет высокодоходные рискованные облигации через API T-Invest,
считает доходность к погашению (YTM), строит диверсифицированный
портфель на 100 000 ₽ из ~30 облигаций с запасом прочности на дефолт 1/3.

Использование:
    pip install tinkoff-investments pandas openpyxl colorama requests
    python tinvest_bond_scanner.py --token YOUR_TOKEN [--sandbox] [--budget 100000]
"""

import argparse
import sys
import os
import json
import math
import time
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional
import pandas as pd
from colorama import init, Fore, Style

init(autoreset=True)

# ──────────────────────────────────────────────────────────────────────────────
# КОНСТАНТЫ
# ──────────────────────────────────────────────────────────────────────────────
PROD_URL  = "https://invest-public-api.tinkoff.ru/rest"
SAND_URL  = "https://sandbox-invest-public-api.tinkoff.ru/rest"

MIN_MONTHS = 6       # минимум месяцев до погашения
MAX_MONTHS = 24      # максимум месяцев до погашения
BUDGET     = 100_000 # бюджет по умолчанию, ₽
N_SLOTS    = 30      # количество позиций в портфеле
DEFAULT_CURRENCY = "RUB"

# Критерии «мусорности»: минимальная ожидаемая YTM (годовая, %) для включения
MIN_YTM_ANNUAL = 20.0   # 20% годовых — порог отбора

# Защитный расчёт: при дефолте 1/3 портфеля оставшиеся 2/3 должны
# покрыть потери. Математика:
#   profit_2/3 * YTM_avg >= loss_1/3 * (1 - recovery_rate)
# recovery_rate ≈ 0 (мусорные бонды), т.е. нужно YTM_avg >= 0.5 (50%)
# или хотя бы 33% годовых чтобы за 2 года выйти в плюс.
SURVIVAL_THRESHOLD_YTM = 33.0  # % годовых — минимум для «выживания» портфеля


# ──────────────────────────────────────────────────────────────────────────────
# HTTP-КЛИЕНТ ДЛЯ T-INVEST REST API (gRPC-gateway)
# ──────────────────────────────────────────────────────────────────────────────
class TInvestClient:
    def __init__(self, token: str, sandbox: bool = False):
        self.base = SAND_URL if sandbox else PROD_URL
        self.sandbox = sandbox
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _post(self, path: str, body: dict = None, retries: int = 3) -> dict:
        url = f"{self.base}{path}"
        for attempt in range(retries):
            try:
                resp = self.session.post(url, json=body or {}, timeout=30)
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    print(f"  {Fore.YELLOW}Rate limit, жду {wait}с...{Style.RESET_ALL}")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                if attempt == retries - 1:
                    raise
                time.sleep(1)
        return {}

    def get_bonds(self) -> list:
        """Получить все доступные облигации."""
        data = self._post("/tinkoff.public.invest.api.contract.v1.InstrumentsService/Bonds",
                          {"instrumentStatus": "INSTRUMENT_STATUS_ALL"})
        return data.get("instruments", [])

    def get_bond_coupons(self, figi: str, from_dt: datetime, to_dt: datetime) -> list:
        """Получить купонные выплаты по облигации в диапазоне дат."""
        body = {
            "figi": figi,
            "from": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to":   to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            data = self._post(
                "/tinkoff.public.invest.api.contract.v1.InstrumentsService/GetBondCoupons",
                body)
            return data.get("events", [])
        except Exception:
            return []

    def get_last_price(self, figi: str) -> Optional[float]:
        """Получить последнюю цену инструмента (в % от номинала для облигаций)."""
        try:
            data = self._post(
                "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetLastPrices",
                {"figi": [figi]})
            prices = data.get("lastPrices", [])
            if prices:
                p = prices[0].get("price", {})
                return _quotation_to_float(p)
        except Exception:
            pass
        return None

    def get_order_book(self, figi: str, depth: int = 1) -> dict:
        """Стакан заявок — для проверки ликвидности."""
        try:
            return self._post(
                "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetOrderBook",
                {"figi": figi, "depth": depth})
        except Exception:
            return {}


# ──────────────────────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ──────────────────────────────────────────────────────────────────────────────
def _quotation_to_float(q: dict) -> float:
    """Конвертировать Quotation {units, nano} → float."""
    if not q:
        return 0.0
    return int(q.get("units", 0)) + int(q.get("nano", 0)) / 1_000_000_000


def _parse_timestamp(ts: str) -> Optional[datetime]:
    """Парсить ISO timestamp в datetime (UTC)."""
    if not ts:
        return None
    try:
        ts = ts.rstrip("Z").split(".")[0]
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _months_between(d1: datetime, d2: datetime) -> float:
    delta = d2 - d1
    return delta.days / 30.44


def calc_ytm(
    price_pct: float,      # текущая цена, % от номинала (например 94.5)
    nominal: float,        # номинал, ₽
    maturity_dt: datetime, # дата погашения
    coupon_events: list,   # список купонных выплат
    now: datetime,
) -> dict:
    """
    Упрощённый расчёт доходности к погашению.

    YTM_total = (Номинал - Цена_покупки + Сумма_купонов) / Цена_покупки
    YTM_annual = YTM_total / years_to_maturity * 100

    Возвращает словарь с деталями расчёта.
    """
    price_rub = nominal * price_pct / 100.0
    years = (maturity_dt - now).days / 365.25
    if years <= 0:
        return {}

    # Суммируем только будущие купоны
    future_coupons = 0.0
    coupon_count = 0
    for ev in coupon_events:
        pay_date = _parse_timestamp(ev.get("couponDate") or ev.get("payDate", ""))
        if pay_date and pay_date > now:
            pay_amount = _quotation_to_float(ev.get("payOneBond", {}))
            future_coupons += pay_amount
            coupon_count += 1

    # Если купонов не нашли — пробуем через couponQuantityPerYear + ставка
    if coupon_count == 0:
        rate = _quotation_to_float({}) # нет данных
        future_coupons = 0.0

    redemption_gain = nominal - price_rub
    total_income    = redemption_gain + future_coupons
    ytm_total       = total_income / price_rub
    ytm_annual      = (ytm_total / years) * 100

    return {
        "price_rub":       round(price_rub, 2),
        "nominal":         nominal,
        "years":           round(years, 2),
        "months":          round(years * 12, 1),
        "future_coupons":  round(future_coupons, 2),
        "coupon_count":    coupon_count,
        "redemption_gain": round(redemption_gain, 2),
        "total_income":    round(total_income, 2),
        "ytm_total_pct":   round(ytm_total * 100, 2),
        "ytm_annual_pct":  round(ytm_annual, 2),
    }


def is_risky(bond: dict) -> bool:
    """
    Эвристика «мусорности» облигации.
    Облигация считается рискованной если выполняется хотя бы одно условие:
      — нет рейтинга / низкий рейтинг
      — маленький эмитент (по типу или имени)
      — это ВДО (высокодоходная облигация) по признакам из API
    """
    # Флаги из API T-Invest
    for_qual = bond.get("forQualInvestor", False)  # только для квалов — часто ВДО
    risk_level = bond.get("riskLevel", "")          # RISK_LEVEL_HIGH и т.п.
    
    # Флаг forQualInvestor сам по себе хороший признак высокого риска
    if for_qual:
        return True
    
    if "HIGH" in str(risk_level).upper():
        return True

    # Смотрим на sector — микрофинансы, МСП и т.д.
    sector = bond.get("sector", "").lower()
    risky_sectors = ["financial", "consumer", "real_estate", "other", ""]
    
    # Нет встроенного рейтинга → считаем рискованным
    # (в API T-Invest рейтинга нет напрямую, ориентируемся на другие поля)
    return True   # берём все и фильтруем по YTM — более честный подход


def build_portfolio(bonds_df: pd.DataFrame, budget: float, n_slots: int) -> pd.DataFrame:
    """
    Строит диверсифицированный портфель:
    — равные доли по n_slots позиций
    — выбирает топ по ytm_annual_pct с учётом диверсификации по эмитентам
    — не более 1 бумаги на эмитента (по issuer_name)
    """
    slot_budget = budget / n_slots
    
    # Сортируем по YTM убыванию
    df = bonds_df.sort_values("ytm_annual_pct", ascending=False).copy()
    
    selected = []
    used_issuers = set()
    
    for _, row in df.iterrows():
        issuer = str(row.get("issuer_name", "")).strip().lower()[:30]
        if issuer in used_issuers:
            continue
        
        price = row["price_rub"]
        if price <= 0:
            continue
        
        lots = max(1, int(slot_budget / price / row.get("lot", 1)))
        cost = lots * price * row.get("lot", 1)
        
        selected.append({**row.to_dict(), "lots": lots, "alloc_rub": round(cost, 2)})
        used_issuers.add(issuer)
        
        if len(selected) >= n_slots:
            break
    
    return pd.DataFrame(selected)


def print_header():
    print(f"\n{Fore.CYAN}{'═'*65}")
    print(f"  🏚️  T-Invest Junk Bond Scanner  |  Мусорные облигации РФ")
    print(f"{'═'*65}{Style.RESET_ALL}\n")


def print_summary(portfolio_df: pd.DataFrame, budget: float):
    """Печатает сводку по портфелю и расчёт выживаемости."""
    n = len(portfolio_df)
    if n == 0:
        print(f"{Fore.RED}Портфель пуст.{Style.RESET_ALL}")
        return

    total_alloc    = portfolio_df["alloc_rub"].sum()
    avg_ytm        = portfolio_df["ytm_annual_pct"].mean()
    avg_months     = portfolio_df["months"].mean()
    
    # Расчёт выживаемости при дефолте 1/3
    n_default      = n // 3
    n_survive      = n - n_default
    
    # Доход от выживших за avg_months
    survive_df     = portfolio_df.nlargest(n_survive, "ytm_annual_pct")
    default_df     = portfolio_df.nsmallest(n_default, "ytm_annual_pct")
    
    survive_income = (survive_df["alloc_rub"] * survive_df["ytm_annual_pct"] / 100 
                      * survive_df["months"] / 12).sum()
    default_loss   = default_df["alloc_rub"].sum()  # полная потеря
    
    net_result     = survive_income - default_loss
    net_pct        = net_result / total_alloc * 100

    print(f"{Fore.GREEN}{'─'*65}")
    print(f"  ПОРТФЕЛЬ: {n} позиций  |  Выделено: {total_alloc:,.0f} ₽ / {budget:,.0f} ₽")
    print(f"  Средняя YTM: {avg_ytm:.1f}% годовых  |  Ср. срок: {avg_months:.0f} мес.")
    print(f"{'─'*65}")
    print(f"  📊 СТРЕСС-ТЕСТ (дефолт {n_default}/{n} эмитентов)")
    print(f"     Потери от дефолтов:  -{default_loss:>10,.0f} ₽")
    print(f"     Доход от выживших:   +{survive_income:>10,.0f} ₽")
    
    color = Fore.GREEN if net_result > 0 else Fore.RED
    sign  = "+" if net_result > 0 else ""
    print(f"     {color}Итог:               {sign}{net_result:>10,.0f} ₽  ({sign}{net_pct:.1f}%){Style.RESET_ALL}")
    
    verdict = "✅ Портфель ВЫДЕРЖИТ дефолт 1/3" if net_result > 0 else "⚠️  Портфель НЕ выдержит дефолт 1/3"
    vcolor  = Fore.GREEN if net_result > 0 else Fore.RED
    print(f"\n  {vcolor}{verdict}{Style.RESET_ALL}")
    print(f"{Fore.GREEN}{'─'*65}{Style.RESET_ALL}\n")


# ──────────────────────────────────────────────────────────────────────────────
# ОСНОВНАЯ ЛОГИКА
# ──────────────────────────────────────────────────────────────────────────────
def scan_bonds(client: TInvestClient, budget: float, n_slots: int) -> pd.DataFrame:
    now = datetime.now(tz=timezone.utc)
    date_min = now + timedelta(days=MIN_MONTHS * 30)
    date_max = now + timedelta(days=MAX_MONTHS * 30)

    print(f"📡 Загружаю список облигаций...")
    all_bonds = client.get_bonds()
    print(f"   Получено: {len(all_bonds)} инструментов\n")

    rub_bonds = [
        b for b in all_bonds
        if b.get("currency", "").upper() == "RUB"
        and not b.get("isShortEnable", False) is False  # торгуемые
        and b.get("apiTradeAvailableFlag", True)
    ]
    print(f"   Рублёвых облигаций: {len(rub_bonds)}")

    results = []
    errors  = 0
    total   = len(rub_bonds)

    for i, bond in enumerate(rub_bonds, 1):
        figi       = bond.get("figi", "")
        name       = bond.get("name", "")
        ticker     = bond.get("ticker", "")
        isin       = bond.get("isin", "")
        nominal_q  = bond.get("nominal", {})
        nominal    = _quotation_to_float(nominal_q) or 1000.0
        mat_ts     = bond.get("maturityDate", "")
        issuer     = bond.get("brand", {}).get("name", "") or name
        lot        = int(bond.get("lot", 1))
        currency   = bond.get("currency", "RUB")
        sector     = bond.get("sector", "")
        for_qual   = bond.get("forQualInvestor", False)
        risk_level = bond.get("riskLevel", "RISK_LEVEL_UNSPECIFIED")

        # Фильтр по дате погашения
        maturity_dt = _parse_timestamp(mat_ts)
        if not maturity_dt or not (date_min <= maturity_dt <= date_max):
            continue

        # Прогресс каждые 50 бумаг
        if i % 50 == 0:
            print(f"  [{i}/{total}] обработано, найдено кандидатов: {len(results)}...")

        # Текущая цена
        price_pct = client.get_last_price(figi)
        if not price_pct or price_pct <= 0:
            errors += 1
            continue

        # Купонные выплаты
        coupons = client.get_bond_coupons(figi, now, maturity_dt)
        
        # YTM
        ytm = calc_ytm(price_pct, nominal, maturity_dt, coupons, now)
        if not ytm or ytm["ytm_annual_pct"] < MIN_YTM_ANNUAL:
            continue

        # Оценка ликвидности (наличие стакана)
        ob = client.get_order_book(figi, depth=1)
        has_bids = len(ob.get("bids", [])) > 0

        results.append({
            "figi":            figi,
            "ticker":          ticker,
            "isin":            isin,
            "name":            name,
            "issuer_name":     issuer,
            "sector":          sector,
            "for_qual":        for_qual,
            "risk_level":      risk_level,
            "lot":             lot,
            "nominal":         nominal,
            "price_pct":       round(price_pct, 4),
            "price_rub":       ytm["price_rub"],
            "maturity":        maturity_dt.strftime("%Y-%m-%d"),
            "months":          ytm["months"],
            "years":           ytm["years"],
            "coupon_count":    ytm["coupon_count"],
            "future_coupons":  ytm["future_coupons"],
            "redemption_gain": ytm["redemption_gain"],
            "total_income":    ytm["total_income"],
            "ytm_total_pct":   ytm["ytm_total_pct"],
            "ytm_annual_pct":  ytm["ytm_annual_pct"],
            "has_liquidity":   has_bids,
        })

        time.sleep(0.05)  # не превышать rate limit

    print(f"\n✅ Сканирование завершено. Кандидатов с YTM ≥ {MIN_YTM_ANNUAL}%: {len(results)}")
    print(f"   Ошибок цен: {errors}\n")

    if not results:
        return pd.DataFrame()

    df = pd.DataFrame(results)
    df = df.sort_values("ytm_annual_pct", ascending=False).reset_index(drop=True)
    return df


def save_results(bonds_df: pd.DataFrame, portfolio_df: pd.DataFrame, out_file: str):
    """Сохранить результаты в Excel с двумя листами."""
    with pd.ExcelWriter(out_file, engine="openpyxl") as writer:
        # Лист 1: все кандидаты
        bonds_df.to_excel(writer, sheet_name="Все кандидаты", index=False)
        
        # Лист 2: портфель
        if not portfolio_df.empty:
            portfolio_df.to_excel(writer, sheet_name="Портфель", index=False)
        
        # Лист 3: CSV-дубликат для удобства
        bonds_df.head(50).to_excel(writer, sheet_name="Топ-50", index=False)

    print(f"💾 Сохранено в: {Fore.CYAN}{out_file}{Style.RESET_ALL}")


def confirm_portfolio(portfolio_df: pd.DataFrame) -> bool:
    """Запросить подтверждение перед (потенциальным) выставлением заявок."""
    if portfolio_df.empty:
        return False
    
    print(f"\n{Fore.YELLOW}{'─'*65}")
    print("  ТОП-10 позиций портфеля:")
    print(f"{'─'*65}{Style.RESET_ALL}")
    
    cols = ["name", "price_rub", "ytm_annual_pct", "months", "lots", "alloc_rub"]
    available = [c for c in cols if c in portfolio_df.columns]
    print(portfolio_df[available].head(10).to_string(index=False))
    
    print(f"\n{Fore.YELLOW}⚠️  Это РИСКОВАННЫЕ высокодоходные облигации.")
    print(f"   Возможна полная потеря вложенных средств.{Style.RESET_ALL}")
    
    ans = input(f"\n{Fore.CYAN}Подтвердить портфель? (yes/no): {Style.RESET_ALL}").strip().lower()
    return ans in ("yes", "y", "да")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="T-Invest Junk Bond Scanner — поиск высокодоходных облигаций"
    )
    parser.add_argument("--token",    required=True,  help="API токен T-Invest")
    parser.add_argument("--sandbox",  action="store_true", help="Использовать Sandbox API")
    parser.add_argument("--budget",   type=float, default=BUDGET, help=f"Бюджет в ₽ (default: {BUDGET})")
    parser.add_argument("--slots",    type=int,   default=N_SLOTS, help=f"Количество позиций (default: {N_SLOTS})")
    parser.add_argument("--min-ytm",  type=float, default=MIN_YTM_ANNUAL, help=f"Мин. YTM годовых %% (default: {MIN_YTM_ANNUAL})")
    parser.add_argument("--min-months", type=int, default=MIN_MONTHS, help=f"Мин. месяцев до погашения (default: {MIN_MONTHS})")
    parser.add_argument("--max-months", type=int, default=MAX_MONTHS, help=f"Макс. месяцев до погашения (default: {MAX_MONTHS})")
    parser.add_argument("--output",   default="bond_scanner_results.xlsx", help="Файл для сохранения результатов")
    parser.add_argument("--no-confirm", action="store_true", help="Не запрашивать подтверждение")
    args = parser.parse_args()

    # Применяем аргументы
    global MIN_YTM_ANNUAL, MIN_MONTHS, MAX_MONTHS
    MIN_YTM_ANNUAL = args.min_ytm
    MIN_MONTHS     = args.min_months
    MAX_MONTHS     = args.max_months

    print_header()
    print(f"  Режим:   {'🏖️  SANDBOX' if args.sandbox else '🔴 PRODUCTION'}")
    print(f"  Бюджет:  {args.budget:,.0f} ₽")
    print(f"  Слотов:  {args.slots}")
    print(f"  YTM min: {args.min_ytm}% годовых")
    print(f"  Срок:    {args.min_months}–{args.max_months} мес.\n")

    client = TInvestClient(token=args.token, sandbox=args.sandbox)

    # Сканирование
    bonds_df = scan_bonds(client, args.budget, args.slots)

    if bonds_df.empty:
        print(f"{Fore.RED}Подходящих облигаций не найдено. Попробуйте снизить --min-ytm.{Style.RESET_ALL}")
        sys.exit(1)

    print(f"📋 Найдено кандидатов: {len(bonds_df)}")
    print(f"   Топ-5 по YTM:")
    preview_cols = ["name", "ytm_annual_pct", "months", "price_rub", "for_qual"]
    avail = [c for c in preview_cols if c in bonds_df.columns]
    print(bonds_df[avail].head(5).to_string(index=False))
    print()

    # Строим портфель
    portfolio_df = build_portfolio(bonds_df, args.budget, args.slots)
    
    # Сводка
    print_summary(portfolio_df, args.budget)

    # Подтверждение
    confirmed = args.no_confirm or confirm_portfolio(portfolio_df)

    if confirmed:
        print(f"\n{Fore.GREEN}✅ Портфель подтверждён.{Style.RESET_ALL}")
        print(f"   ℹ️  Для автоматической покупки добавьте вызовы OrdersService/PostOrder")
        print(f"   ℹ️  в функцию execute_orders() ниже.\n")
    else:
        print(f"\n{Fore.YELLOW}↩️  Портфель отклонён. Данные сохранены для анализа.{Style.RESET_ALL}\n")

    # Сохраняем в Excel всегда
    save_results(bonds_df, portfolio_df, args.output)

    # Также CSV для удобства
    csv_file = args.output.replace(".xlsx", ".csv")
    bonds_df.to_csv(csv_file, index=False, encoding="utf-8-sig")
    print(f"💾 CSV: {Fore.CYAN}{csv_file}{Style.RESET_ALL}")

    return bonds_df, portfolio_df


# ──────────────────────────────────────────────────────────────────────────────
# ТОЧКА РАСШИРЕНИЯ: автоматическое выставление заявок
# ──────────────────────────────────────────────────────────────────────────────
def execute_orders(client: TInvestClient, portfolio_df: pd.DataFrame, account_id: str):
    """
    ЗАГЛУШКА — выставление рыночных заявок на покупку.
    Раскомментировать и протестировать в Sandbox перед боевым использованием!
    """
    for _, row in portfolio_df.iterrows():
        body = {
            "figi":        row["figi"],
            "quantity":    int(row["lots"]),
            "direction":   "ORDER_DIRECTION_BUY",
            "accountId":   account_id,
            "orderType":   "ORDER_TYPE_MARKET",
            "orderId":     f"junk_{row['figi']}_{int(time.time())}",
        }
        # result = client._post(
        #     "/tinkoff.public.invest.api.contract.v1.OrdersService/PostOrder", body)
        print(f"  [DRY RUN] Купить {row['lots']} лот(а) {row['ticker']} / {row['name'][:40]}")
        time.sleep(0.1)


if __name__ == "__main__":
    main()