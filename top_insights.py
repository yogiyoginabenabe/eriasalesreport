"""TOP dashboard area comparison, using the same MTD scope as the KPI cards."""

import pandas as pd
import math


def classify(sales_pct, visitor_pct):
    if sales_pct is None or visitor_pct is None:
        return "比較データ不足"
    if sales_pct >= 100 and visitor_pct >= 100:
        return "好調"
    if sales_pct < 100 and visitor_pct < 100:
        return "要確認"
    return "売上達成・客数減" if sales_pct >= 100 else "集客維持・売上未達"


def build_area_insights(master, actual, previous, targets, cutoff, activity=None, previous_activity=None):
    """Return area summaries and store rows; missing comparisons stay unclassified."""
    result = {}
    actual = actual if not actual.empty else pd.DataFrame(columns=["店舗名", "指標", "値"])
    previous = previous if not previous.empty else pd.DataFrame(columns=["店舗名", "指標", "値"])

    def total(frame, names, metric):
        subset = frame[(frame["店舗名"].isin(names)) & (frame["指標"] == metric)]
        return float(pd.to_numeric(subset["値"], errors="coerce").sum())

    def ratio(value, baseline):
        return value / baseline * 100 if value is not None and baseline > 0 and math.isfinite(baseline) else None

    def target_for(name):
        prefix = f"{name}_受注金額(税抜)_"
        daily = 0.0
        found = False
        for key, value in targets.items():
            if not key.startswith(prefix):
                continue
            date = pd.to_datetime(key[len(prefix):], errors="coerce")
            if pd.notna(date) and date.year == cutoff.year and date.month == cutoff.month and date.date() <= cutoff:
                try:
                    daily += float(value)
                    found = True
                except (ValueError, TypeError):
                    pass
        if found:
            return daily
        try:
            return float(targets.get(f"{name}_受注金額(税抜)", 0) or 0)
        except (ValueError, TypeError):
            return 0.0

    for area in ("渡邊_A", "渡邊_B"):
        names = master.loc[master["AM名"] == area, "店舗名"].dropna().tolist()
        rows = []
        for name in names:
            sales = total(actual, [name], "受注金額(税抜)")
            visitors = total(actual, [name], "客数")
            prior_visitors = total(previous, [name], "客数")
            target = target_for(name)
            sales_pct, visitor_pct = ratio(sales, target), ratio(visitors, prior_visitors)
            present = set(actual.loc[actual["店舗名"] == name, "指標"])
            if "受注金額(税抜)" not in present:
                sales_pct = None
            if "客数" not in present:
                visitor_pct = None
            status = classify(sales_pct, visitor_pct)
            seats = total(actual, [name], "座数")
            prior_seats = total(previous, [name], "座数")
            pieces = total(actual, [name], "品数")
            prior_pieces = total(previous, [name], "品数")
            signals = []
            from cast_insights import activity_value, activity_yoy
            steps = activity_value(activity, [name], "8ステップ数")
            prior_steps = activity_value(previous_activity, [name], "8ステップ数")
            steps_yoy = activity_yoy(steps, prior_steps)
            hours = activity_value(activity, [name], "勤務時間(h)")
            if steps is not None:
                signals.append(f"8ステップ{steps:,.0f}回" + (f"・前年比{steps_yoy:.0f}%" if steps_yoy is not None else ""))
            if hours is not None:
                signals.append(f"勤務時間{hours:,.1f}h")
            if prior_seats > 0:
                signals.append(f"座数前年比{seats / prior_seats * 100:.0f}%")
            current_cvr = actual[(actual["店舗名"] == name) & (actual["指標"] == "CVR")]
            if not current_cvr.empty:
                cvr_value = pd.to_numeric(current_cvr["値"], errors="coerce").dropna()
                if not cvr_value.empty:
                    signals.append(f"CVR実績{cvr_value.mean():.1f}%")
            if visitors > 0 and prior_visitors > 0:
                # Ticket is derived from totals, not an average of daily ratios.
                prior_sales = total(previous, [name], "受注金額(税抜)")
                if pieces > 0 and prior_pieces > 0:
                    signals.append(f"品数前年比{pieces / prior_pieces * 100:.0f}%")
                if prior_sales > 0:
                    signals.append(f"客単価前年比{(sales / visitors) / (prior_sales / prior_visitors) * 100:.0f}%")
            reasons = []
            if steps_yoy is not None and prior_seats > 0 and steps_yoy < 100 and seats < prior_seats:
                reasons.append("8ステップと座数がともに前年を下回っています。体験提供の活動量を確認してください。")
            if prior_seats > 0 and prior_visitors > 0:
                seat_yoy = seats / prior_seats * 100
                if seat_yoy < 100 and visitor_pct is not None and visitor_pct < 100:
                    reasons.append("座数と購入客数がともに前年を下回っています。体験提供の状況を確認したい店舗です。")
                elif seat_yoy >= 100 and visitor_pct is not None and visitor_pct < 100:
                    reasons.append("座数は前年を維持していますが購入客数が減っています。体験から購入へのつながりを確認したい店舗です。")
                elif seat_yoy >= 100 and visitor_pct is not None and visitor_pct >= 100:
                    reasons.append("座数と購入客数がともに前年以上で推移しています。")
            prior_sales = total(previous, [name], "受注金額(税抜)")
            if visitors > 0 and prior_visitors > 0 and prior_sales > 0:
                ticket_yoy = (sales / visitors) / (prior_sales / prior_visitors) * 100
                if ticket_yoy < 100:
                    reasons.append("客単価が前年を下回っています。購入商品の構成やセット提案を確認する余地があります。")
                elif ticket_yoy > 100:
                    reasons.append("客単価の前年超えが、受注実績を支えています。")
            rows.append({"name": name, "sales": sales, "visitors": visitors,
                         "target": target, "sales_pct": sales_pct,
                         "visitor_pct": visitor_pct, "status": status,
                         "steps": steps, "steps_yoy": steps_yoy, "hours": hours,
                         "reason": " ".join(reasons) or "補助KPIだけでは背景を判断できません。日報と店舗状況の確認が必要です。",
                         "signals": "・".join(signals) or "補助KPIの比較データなし"})
        area_sales = sum(r["sales"] for r in rows)
        area_target = sum(r["target"] for r in rows)
        target_complete = bool(rows) and all(r["sales_pct"] is not None for r in rows)
        visitors_complete = bool(rows) and all(r["visitor_pct"] is not None for r in rows)
        area_visitors = sum(r["visitors"] for r in rows)
        area_prior_visitors = total(previous, names, "客数")
        result[area] = {"stores": rows, "sales": area_sales,
                        "sales_pct": ratio(area_sales, area_target) if target_complete else None,
                        "visitor_pct": ratio(area_visitors, area_prior_visitors) if visitors_complete else None,
                        "visitors": area_visitors}
    return result
