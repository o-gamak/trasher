#!/usr/bin/env python3
import sys
import os
import time
import json
import logging
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

from colorama import init, Fore, Style
import hydra
from omegaconf import DictConfig, OmegaConf
from dotenv import load_dotenv

# Настройка путей
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_JSON_RAW_PATH = _DATA_DIR / "download.json"
_CSV_PARSED_PATH = _DATA_DIR / "download_data.csv"
_READY_DATA_PATH = _DATA_DIR / "ready_data.csv"

load_dotenv(_PROJECT_ROOT / ".env", override=False)
init(autoreset=True)
log = logging.getLogger(__name__)

PROD_URL = "https://invest-public-api.tinkoff.ru/rest"
SAND_URL = "https://sandbox-invest-public-api.tinkoff.ru/rest"

class TInvestClient:
    def __init__(self, cfg: DictConfig):
        sandbox = cfg.api.sandbox
        token = os.getenv("TINVEST_TOKEN") or os.getenv("TINVEST_SANDBOX_TOKEN") or \
                OmegaConf.select(cfg, "api.sandbox_token") or OmegaConf.select(cfg, "api.token")
        
        if not token:
            log.error("Токен не найден!"); sys.exit(1)

        self.base = SAND_URL if sandbox else PROD_URL
        self.timeout = cfg.api.timeout
        self.retries = cfg.api.retries
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    def _post(self, path: str, body: dict = None) -> dict:
        url = f"{self.base}{path}"
        for attempt in range(self.retries):
            try:
                resp = self.session.post(url, json=body or {}, timeout=self.timeout)
                if resp.status_code == 429:
                    time.sleep(2 ** (attempt + 1)); continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                if attempt == self.retries - 1: raise e
                time.sleep(1)
        return {}

# ──────────────────────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ──────────────────────────────────────────────────────────────────────────────
def _quotation_to_float(q):
    if not q: return 0.0
    return int(q.get("units", 0)) + int(q.get("nano", 0)) / 1_000_000_000

def _parse_timestamp(ts):
    if not ts: return None
    return datetime.fromisoformat(ts.rstrip("Z").split(".")[0]).replace(tzinfo=timezone.utc)

def calc_ytm(price_pct, nominal, maturity_dt, coupons, now):
    price_rub = nominal * price_pct / 100.0
    if price_rub <= 0: return None
    future_coupons = sum(_quotation_to_float(c.get("payOneBond")) for c in coupons 
                        if (d := _parse_timestamp(c.get("couponDate") or c.get("payDate"))) and d > now)
    years = (maturity_dt - now).days / 365.25
    if years <= 0: return None
    ytm = (((nominal - price_rub) + future_coupons) / price_rub) / years * 100
    return {"price_rub": round(price_rub, 2), "months": round(years * 12, 1), 
            "ytm_annual_pct": round(ytm, 2), "total_income": round(future_coupons + (nominal - price_rub), 2)}

def calc_junk_score(row, cfg):
    ytm = row.get("ytm_annual_pct", 0)
    score = min(50, (ytm / cfg.scanner.junk_ytm_threshold) * 50)
    if 9 <= row.get("months", 0) <= 15: score += 30
    if row.get("price_pct", 100) < 90: score += 20
    return round(score, 2)

# ──────────────────────────────────────────────────────────────────────────────
# ПРОЦЕССИНГ ДАННЫХ
# ──────────────────────────────────────────────────────────────────────────────
def scan_bonds(client: TInvestClient, cfg: DictConfig) -> pd.DataFrame:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    # Режим чтения из кеша
    if not cfg.app.get("load_data", True):
        if _CSV_PARSED_PATH.exists():
            print(f"📦 {Fore.CYAN}Загрузка из CSV: {_CSV_PARSED_PATH}")
            return pd.read_csv(_CSV_PARSED_PATH)
        elif _JSON_RAW_PATH.exists():
            print(f"📦 {Fore.YELLOW}CSV нет, восстанавливаю из JSON...")
        else:
            print(f"{Fore.RED}Кэш не найден!"); sys.exit(1)

    # Режим загрузки/парсинга
    now = datetime.now(tz=timezone.utc)
    raw_storage = []

    if cfg.app.get("load_data", True):
        print("📡 Запрос к API T-Invest...")
        bonds_raw = client._post("/tinkoff.public.invest.api.contract.v1.InstrumentsService/Bonds", 
                                {"instrumentStatus": "INSTRUMENT_STATUS_ALL"}).get("instruments", [])
        
        # Фильтр по валюте и сроку до обработки купонов (чтобы не дудосить API)
        candidates = [b for b in bonds_raw if b.get("currency") == cfg.scanner.currency and 
                      (m := _parse_timestamp(b.get("maturityDate"))) and 
                      now + timedelta(days=cfg.scanner.min_months*30) <= m <= now + timedelta(days=cfg.scanner.max_months*30)]

        price_map = {}
        figis = [b["figi"] for b in candidates]
        for i in range(0, len(figis), cfg.api.price_batch_size):
            batch = figis[i : i + cfg.api.price_batch_size]
            prices = client._post("/tinkoff.public.invest.api.contract.v1.MarketDataService/GetLastPrices", {"figi": batch})
            price_map.update({p["figi"]: _quotation_to_float(p.get("price")) for p in prices.get("lastPrices", [])})
            time.sleep(cfg.api.price_batch_delay)

        results = []
        for i, b in enumerate(candidates, 1):
            figi = b["figi"]
            price = price_map.get(figi)
            if not price: continue
            
            try:
                mat = _parse_timestamp(b["maturityDate"])
                coups = client._post("/tinkoff.public.invest.api.contract.v1.InstrumentsService/GetBondCoupons", 
                                    {"figi": figi, "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "to": mat.strftime("%Y-%m-%dT%H:%M:%SZ")}).get("events", [])
                
                ytm_d = calc_ytm(price, _quotation_to_float(b.get("nominal")), mat, coups, now)
                
                # Добавляем в RAW JSON (всё что пришло)
                raw_entry = {"instrument": b, "price": price, "coupons": coups, "calc": ytm_d}
                raw_storage.append(raw_entry)

                if ytm_d:
                    # ОГРАНИЧЕНИЕ ПО ДОХОДНОСТИ
                    if cfg.scanner.min_ytm_annual <= ytm_d["ytm_annual_pct"] <= cfg.scanner.get("max_ytm_annual", 100.0):
                        row = {**ytm_d, "figi": figi, "ticker": b["ticker"], "name": b["name"], 
                               "issuer_name": (b.get("brand") or {}).get("name") or b["name"], 
                               "lot": int(b["lot"]), "price_pct": price, "for_qual": b.get("forQualInvestor")}
                        row["junk_score"] = calc_junk_score(row, cfg)
                        results.append(row)
                
                time.sleep(cfg.api.request_delay)
            except Exception as e: continue
            if i % 50 == 0: print(f"  [{i}/{len(candidates)}] обработано...")

        # Сохраняем L1: JSON
        with open(_JSON_RAW_PATH, "w", encoding="utf-8") as f:
            json.dump(raw_storage, f, ensure_ascii=False, indent=4)
        print(f"💾 L1 сохранено: {_JSON_RAW_PATH}")

        # Сохраняем L2: Parsed CSV
        df = pd.DataFrame(results)
        df.to_csv(_CSV_PARSED_PATH, index=False, encoding="utf-8-sig")
        print(f"💾 L2 сохранено: {_CSV_PARSED_PATH}")
        return df

@hydra.main(config_path="../../config", config_name="config", version_base=None)
def main(cfg: DictConfig):
    print(f"\n{Fore.YELLOW}🏚️ T-Invest Junk Scanner | Max YTM: {cfg.scanner.get('max_ytm_annual', 100)}%")
    
    client = TInvestClient(cfg)
    df = scan_bonds(client, cfg)
    
    if df.empty:
        print(f"{Fore.RED}Нет данных."); return

    # Сборка портфеля (L3)
    budget_per_slot = cfg.portfolio.budget / cfg.portfolio.slots
    selected, issuers = [], {}
    for _, row in df.sort_values("junk_score", ascending=False).iterrows():
        iss = str(row['issuer_name'])[:10].lower()
        if issuers.get(iss, 0) >= cfg.portfolio.max_per_issuer: continue
        cost = row['price_rub'] * row['lot']
        if cost > budget_per_slot * 2: continue
        lots = max(1, int(budget_per_slot / cost))
        r = row.to_dict()
        r.update({'lots': lots, 'alloc_rub': round(lots * cost, 2)})
        selected.append(r); issuers[iss] = issuers.get(iss, 0) + 1
        if len(selected) >= cfg.portfolio.slots: break

    portfolio = pd.DataFrame(selected)
    print(f"\n{Fore.GREEN}Финальный портфель (топ-15):")
    print(portfolio[['name', 'ytm_annual_pct', 'junk_score', 'alloc_rub']].head(15))

    # Сохранение L3
    portfolio.to_csv(_READY_DATA_PATH, index=False, encoding="utf-8-sig")
    print(f"\n✅ Результаты в: {_READY_DATA_PATH}")

if __name__ == "__main__":
    main()