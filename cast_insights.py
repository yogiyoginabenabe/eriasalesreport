"""店舗別キャスト活動。未取得と実績ゼロを区別する。"""
import pandas as pd


def activity_windows(frame, cutoff, stores):
    columns = ["店舗名", "指標", "値", "日付"]
    if frame.empty:
        return pd.DataFrame(columns=columns), pd.DataFrame(columns=columns)
    data = frame.copy()
    data["日付"] = pd.to_datetime(data["日付"], errors="coerce")
    data["値"] = pd.to_numeric(data["値"], errors="coerce")
    data = data.dropna(subset=["日付", "指標", "値"])
    data = data[data["店舗名"].isin(stores)]
    start = pd.Timestamp(cutoff.replace(day=1))
    end = pd.Timestamp(cutoff)
    current = data[data["日付"].between(start, end)]
    previous = data[data["日付"].between(start - pd.Timedelta(weeks=52), end - pd.Timedelta(weeks=52))]
    return current.copy(), previous.copy()


def activity_value(frame, names, metric):
    if frame is None or frame.empty:
        return None
    rows = frame[frame["店舗名"].isin(names) & (frame["指標"] == metric)]
    # 未取得の店舗をゼロとして合算しない。
    if not set(names).issubset(set(rows["店舗名"])):
        return None
    return float(pd.to_numeric(rows["値"], errors="coerce").sum())


def activity_yoy(current, previous):
    return current / previous * 100 if current is not None and previous is not None and previous > 0 else None
