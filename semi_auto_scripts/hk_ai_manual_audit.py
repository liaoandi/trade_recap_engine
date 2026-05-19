#!/usr/bin/env python3
import pandas as pd
import yfinance as yf

# Fetch exact data around the first trade (Jan 23 to Jan 26)
print("--- Auditing First Trade (Jan 23 - Jan 27) ---")
df1 = yf.download("2513.HK", start="2026-01-22", end="2026-01-28", interval="1d", progress=False)
if isinstance(df1.columns, pd.MultiIndex):
    df1.columns = df1.columns.get_level_values(0)
print(df1[['Open', 'High', 'Low', 'Close', 'Volume']].to_string())

# Fetch exact data around the second trade (Feb 10 to Feb 14)
print("\n--- Auditing Second Trade (Feb 10 - Feb 14) ---")
df2 = yf.download("2513.HK", start="2026-02-10", end="2026-02-15", interval="1d", progress=False)
if isinstance(df2.columns, pd.MultiIndex):
    df2.columns = df2.columns.get_level_values(0)
print(df2[['Open', 'High', 'Low', 'Close', 'Volume']].to_string())

# Fetch exact data around the third trade (Mar 12 to Mar 18)
print("\n--- Auditing Third Trade (Mar 12 - Mar 19) ---")
df3 = yf.download("2513.HK", start="2026-03-12", end="2026-03-19", interval="1d", progress=False)
if isinstance(df3.columns, pd.MultiIndex):
    df3.columns = df3.columns.get_level_values(0)
print(df3[['Open', 'High', 'Low', 'Close', 'Volume']].to_string())
