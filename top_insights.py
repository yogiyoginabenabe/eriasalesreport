"""TOP dashboard area comparison, using the same MTD scope as the KPI cards."""

import pandas as pd


def build_area_insights(master, actual, previous, targets, cutoff):
    """Return area summaries and store rows; missing comparisons stay unclassified."""
    result = {}
    actual = actual if not actual.empty else pd.DataFrame(columns=["店舗名", "指標", "値"])
    previous = previous if not previous.empty else pd.DataFrame(columns=["店舗名", "指標", "値"])

    def total(frame, names, metric):
        subset = frame[(frame["店舗名"].isin(names)) & (frame["指標"] == metric)]
        return float(pd.to_numeric(subset["値"], errors="coerce").sum())

    def ratio(value, baseline):
        return value / baseline * 100 if baseline > 0 else None

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
            if sales_pct is None or visitor_pct is None:
                status = "比較データ不足"
            elif sales_pct >= 100 and visitor_pct >= 100:
                status = "好調"
            elif sales_pct < 100 and visitor_pct < 100:
                status = "要確認"
            elif sales_pct >= 100:
                status = "売上達成・客数減"
            else:
                status = "集客維持・売上未達"
            seats = total(actual, [name], "座数")
            prior_seats = total(previous, [name], "座数")
            pieces = total(actual, [name], "品数")
            prior_pieces = total(previous, [name], "品数")
            signals = []
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
            rows.append({"name": name, "sales": sales, "visitors": visitors,
                         "target": target, "sales_pct": sales_pct,
                         "visitor_pct": visitor_pct, "status": status,
                         "signals": "・".join(signals) or "補助KPIの比較データなし"})
        area_sales = sum(r["sales"] for r in rows)
        area_target = sum(r["target"] for r in rows)
        area_visitors = sum(r["visitors"] for r in rows)
        area_prior_visitors = total(previous, names, "客数")
        result[area] = {"stores": rows, "sales": area_sales,
                        "sales_pct": ratio(area_sales, area_target),
                        "visitor_pct": ratio(area_visitors, area_prior_visitors),
                        "visitors": area_visitors}
    return result
