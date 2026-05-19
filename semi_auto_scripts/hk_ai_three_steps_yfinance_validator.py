#!/usr/bin/env python3
import pandas as pd
import yfinance as yf

def validate_three_steps_strategy(ticker, target_profit_pct=20.0):
    print(f"\n=========================================")
    print(f"Validating 'Three Steps to Catch a Turn' Strategy for {ticker}")
    print(f"=========================================\n")
    
    try:
        # Fetch data since IPO
        df = yf.download(ticker, period="3mo", interval="1d", progress=False)
        
        if df.empty:
            print(f"No data found for {ticker}.")
            return
            
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            
        df.dropna(inplace=True)
        if len(df) < 5: 
             print(f"Not enough data for {ticker}. Only {len(df)} days available.")
             return

        print(f"Data available from {df.index[0].date()} to {df.index[-1].date()} ({len(df)} trading days)")

        # Calculate a 3-day average volume to identify "low volume" (using 3-day because history is very short)
        df['Avg_Vol_3'] = df['Volume'].rolling(window=3).mean()

        total_trades = 0
        winning_trades = 0
        total_pnl_pct = 0.0
        
        in_position = False
        entry_price = 0.0
        entry_date = None
        stop_loss_price = 0.0
        
        print("\n--- Trade Log ---")
        
        for i in range(3, len(df)):
            today = df.iloc[i]
            yesterday = df.iloc[i-1]
            day_before = df.iloc[i-2]
            
            # Context: A recent down day
            yesterday_is_down = yesterday['Close'] < yesterday['Open']
            
            # --- Step 2: Ice point ---
            # Volume dried up compared to the 3-day moving average
            volume_dried_up = yesterday['Volume'] < yesterday['Avg_Vol_3']
            is_ice_point = yesterday_is_down and volume_dried_up
            
            # --- Step 3: Strong Bullish Confirmation ---
            today_is_up = today['Close'] > today['Open']
            
            # Reversal: Today's close must be higher than yesterday's open (engulfing the down body)
            is_reversal = today['Close'] > yesterday['Open']
            
            # Volume confirmation: Today's volume > Yesterday's volume
            volume_expanded = today['Volume'] > yesterday['Volume']
            
            trigger_hit = is_ice_point and today_is_up and is_reversal and volume_expanded

            if not in_position and trigger_hit:
                in_position = True
                entry_price = today['Close']
                entry_date = df.index[i].date()
                stop_loss_price = yesterday['Low']
                
                print(f"BUY  | Date: {entry_date} | Price: {entry_price:.2f} | Stop Loss: {stop_loss_price:.2f}")

            elif in_position:
                if today['Low'] < stop_loss_price:
                    in_position = False
                    exit_price = stop_loss_price
                    exit_date = df.index[i].date()
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    total_trades += 1
                    total_pnl_pct += pnl_pct
                    print(f"STOP | Date: {exit_date} | Price: {exit_price:.2f} | PnL: {pnl_pct:.2f}%")
                
                elif today['High'] >= entry_price * (1 + target_profit_pct / 100.0):
                    in_position = False
                    exit_price = entry_price * (1 + target_profit_pct / 100.0)
                    exit_date = df.index[i].date()
                    pnl_pct = target_profit_pct
                    total_trades += 1
                    winning_trades += 1
                    total_pnl_pct += pnl_pct
                    print(f"SELL | Date: {exit_date} | Price: {exit_price:.2f} | PnL: +{pnl_pct:.2f}% (Target Hit)")

        if in_position:
             current_pnl = ((df['Close'].iloc[-1] - entry_price) / entry_price) * 100
             print(f"OPEN | Date: {df.index[-1].date()} | Current Price: {df['Close'].iloc[-1]:.2f} | Unrealized PnL: {current_pnl:.2f}%")

        win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0
        
        print(f"\n--- Strategy Summary ---")
        print(f"Total Completed Trades: {total_trades}")
        print(f"Win Rate: {win_rate:.2f}%")
        print(f"Cumulative Realized PnL: {total_pnl_pct:.2f}%")

    except Exception as e:
        print(f"Error processing {ticker}: {e}")

if __name__ == "__main__":
    validate_three_steps_strategy("2513.HK") # Zhipu with correct yfinance ticker format
