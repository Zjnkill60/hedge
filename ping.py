"""
Hedge/Arbitrage Bot: Lighter vs MEXC (Full WebSocket)
So sánh orderbook realtime giữa 2 sàn để tìm cơ hội chênh lệch giá

Configuration: Sửa các thông tin bên dưới
"""

import asyncio
import json
import websocket
import threading
import aiohttp
from typing import Dict, Optional, Tuple, List
from datetime import datetime
from collections import deque
import time
import requests

# ============================================================================
# 📝 CONFIGURATION - SỬA Ở ĐÂY
# ============================================================================

# Telegram Settings
TELEGRAM_ENABLED = True  # True = bật Telegram, False = tắt
TELEGRAM_BOT_TOKEN = "8410590021:AAEuXtNaXMk7-Su2oO20N_1l4-3KwZ_1H5g"  # Nhập Bot Token của bạn
TELEGRAM_CHAT_IDS = ["1982844680", "1056814691"]   # Nhiều ID ở đây!

# Alert Settings
MIN_SPREAD_USD = 10.0    # Chênh lệch tối thiểu để gửi alert (USD)
ALERT_COOLDOWN = 60      # Cooldown giữa các alert (giây)

# Trading Settings
MARKET_CHOICE = "BTC"    # "BTC" hoặc "ETH"
TRADE_SIZE = 1         # Kích thước giao dịch test (0.1 BTC hoặc 1 ETH)

# Update Settings
UPDATE_INTERVAL = 0.01   # Tần suất update (giây)

# ============================================================================


class TelegramNotifier:
    """Gửi thông báo qua Telegram Bot - hỗ trợ nhiều chat_id"""
    def __init__(self, bot_token: str, chat_ids: List[str], cooldown_seconds: int = 60):
        self.bot_token = bot_token
        self.chat_ids = [str(cid).strip() for cid in chat_ids if str(cid).strip()]  # List nhiều ID
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.last_notification_time = {}  # direction → thời gian gửi gần nhất (per direction)
        self.cooldown_seconds = cooldown_seconds
        
        if not self.chat_ids:
            print("[Telegram] Warning: Không có chat_id hợp lệ!")
    
    async def send_message(self, message: str, parse_mode: str = "HTML"):
        """Gửi message tới tất cả chat_id"""
        if not self.chat_ids:
            return False
            
        success_count = 0
        async with aiohttp.ClientSession() as session:
            for chat_id in self.chat_ids:
                try:
                    payload = {
                        "chat_id": chat_id,
                        "text": message,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True
                    }
                    async with session.post(self.api_url, json=payload, timeout=10) as resp:
                        if resp.status == 200:
                            success_count += 1
                        else:
                            text = await resp.text()
                            print(f"[Telegram] Lỗi gửi tới {chat_id}: {resp.status} - {text}")
                except Exception as e:
                    print(f"[Telegram] Exception khi gửi tới {chat_id}: {e}")
        
        return success_count > 0
    
    def should_notify(self, direction: str) -> bool:
        now = datetime.now()
        last_time = self.last_notification_time.get(direction)
        if not last_time or (now - last_time).total_seconds() >= self.cooldown_seconds:
            return True
        return False
    
    def mark_notified(self, direction: str):
        self.last_notification_time[direction] = datetime.now()
    
    async def send_arbitrage_alert(self, opportunity: Dict, trade_size: float, unit_name: str):
        direction = opportunity['direction']
        
        if not self.should_notify(direction):
            return False
        
        # ... (giữ nguyên phần tạo message như cũ)
        spread = opportunity['profit_per_unit']
        spread_pct = opportunity['spread_pct']
        buy_price = opportunity['buy_price']
        sell_price = opportunity['sell_price']
        buy_exchange = opportunity['buy_exchange']
        sell_exchange = opportunity['sell_exchange']
        
        if spread > 50:
            emoji = "🚨💰🚨"
            level = "RẤT LỚN"
        elif spread > 20:
            emoji = "🔥💵"
            level = "LỚN"
        elif spread > 10:
            emoji = "✅💸"
            level = "VỪA"
        else:
            emoji = "⚠️"
            level = "NHỎ"
        
        message = f"""
{emoji} <b>ARBITRAGE OPPORTUNITY - {level}</b> {emoji}

<b>🎯 Strategy:</b>
• Buy {buy_exchange}: <code>${buy_price:,.2f}</code>
• Sell {sell_exchange}: <code>${sell_price:,.2f}</code>

<b>💰 Profit:</b>
• Chênh lệch: <b>+${spread:,.2f}</b> ({spread_pct:+.4f}%)
• Với {trade_size} {unit_name}: <b>+${spread * trade_size:,.2f}</b>

"""
        
        success = await self.send_message(message)
        if success:
            self.mark_notified(direction)
            print(f"\n✅ [Telegram] Đã gửi alert tới {len(self.chat_ids)} người: {direction} (+${spread:,.2f})")
        
        return success


class OrderBook:
    """Lưu trữ orderbook của một sàn"""
    def __init__(self, exchange_name: str):
        self.exchange = exchange_name
        self.bids = []  # [[price, quantity], ...]
        self.asks = []  # [[price, quantity], ...]
        self.last_update = None
        
    def update(self, bids, asks):
        """Cập nhật full orderbook"""
        self.bids = sorted(bids, key=lambda x: float(x[0]), reverse=True)
        self.asks = sorted(asks, key=lambda x: float(x[0]))
        self.last_update = datetime.now()
    
    def get_best_bid(self) -> Optional[Tuple[float, float]]:
        """Giá mua cao nhất"""
        if self.bids:
            return float(self.bids[0][0]), float(self.bids[0][1])
        return None
    
    def get_best_ask(self) -> Optional[Tuple[float, float]]:
        """Giá bán thấp nhất"""
        if self.asks:
            return float(self.asks[0][0]), float(self.asks[0][1])
        return None
    
    def calculate_market_impact(self, side: str, size: float) -> Tuple[float, float]:
        """
        Tính giá trung bình và slippage khi thực hiện lệnh market
        
        Args:
            side: 'buy' hoặc 'sell'
            size: Số lượng cần giao dịch
        
        Returns:
            (avg_price, total_cost)
        """
        levels = self.asks if side == 'buy' else self.bids
        
        remaining = size
        total_cost = 0
        filled = 0
        
        for price, quantity in levels:
            price = float(price)
            quantity = float(quantity)
            
            if remaining <= 0:
                break
            
            fill_qty = min(remaining, quantity)
            total_cost += price * fill_qty
            filled += fill_qty
            remaining -= fill_qty
        
        if filled == 0:
            return 0, 0
        
        avg_price = total_cost / filled
        return avg_price, total_cost


class LighterWebSocketClient:
    """Client WebSocket chuẩn cho Lighter (giống 100% Web UI)"""

    def __init__(self, market_index: int = 0):
        self.market_index = market_index
        self.orderbook = OrderBook("Lighter")
        self.ws_url = "wss://mainnet.zklighter.elliot.ai/stream"
        self.snapshot_url = f"https://mainnet.zklighter.elliot.ai/depth/{market_index}"

        self.bids = {}  # price -> size
        self.asks = {}

        self.ws = None
        self.thread = None
        self.running = False

    # -----------------------------------------------------------------
    # LOAD SNAPSHOT
    # -----------------------------------------------------------------
    def load_snapshot(self):
        try:
            import requests
            r = requests.get(self.snapshot_url, timeout=3)
            data = r.json()

            # RESET trước khi load
            self.bids.clear()
            self.asks.clear()

            self.bids = {float(p): float(s) for p, s in data.get("bids", [])}
            self.asks = {float(p): float(s) for p, s in data.get("asks", [])}

            bids_list = [[str(p), str(s)] for p, s in self.bids.items()]
            asks_list = [[str(p), str(s)] for p, s in self.asks.items()]
            self.orderbook.update(bids_list, asks_list)

            print("[Lighter] Snapshot loaded OK")
        except Exception as e:
            print("[Lighter] Snapshot ERROR:", e)

    # -----------------------------------------------------------------
    # START CLIENT
    # -----------------------------------------------------------------
    def start(self):
        self.running = True
        self.load_snapshot()

        self.thread = threading.Thread(target=self._ws_loop, daemon=True)
        self.thread.start()

    # -----------------------------------------------------------------
    # WS LOOP (auto reconnect)
    # -----------------------------------------------------------------
    def _ws_loop(self):
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )

                # RẤT QUAN TRỌNG → Lighter cần ping
                self.ws.run_forever(
                    ping_interval=15,
                    ping_timeout=5,
                    reconnect=5
                )

            except Exception as e:
                print("[Lighter] WS fatal error:", e)

            print("[Lighter] Retry connection in 3s...")
            time.sleep(3)

    # -----------------------------------------------------------------
    # HANDLERS
    # -----------------------------------------------------------------
    def _on_open(self, ws):
        print("[Lighter] Connected WS")
        subscribe_msg = {
            "type": "subscribe",
            "channel": f"order_book/{self.market_index}"
        }
        try:
            ws.send(json.dumps(subscribe_msg))
        except:
            pass

    def _on_message(self, ws, message):
        try:
            msg = json.loads(message)
            if msg.get("type") != "update/order_book":
                return

            ob = msg.get("order_book", {})
            if not ob:
                return

            # APPLY DELTAS
            for level in ob.get("asks", []):
                p = float(level["price"])
                s = float(level["size"])
                if s <= 0:
                    self.asks.pop(p, None)
                else:
                    self.asks[p] = s

            for level in ob.get("bids", []):
                p = float(level["price"])
                s = float(level["size"])
                if s <= 0:
                    self.bids.pop(p, None)
                else:
                    self.bids[p] = s

            # SORT & UPDATE
            bids_sorted = sorted(self.bids.items(), key=lambda x: -x[0])
            asks_sorted = sorted(self.asks.items(), key=lambda x: x[0])

            self.orderbook.update(
                [[str(p), str(s)] for p, s in bids_sorted],
                [[str(p), str(s)] for p, s in asks_sorted]
            )

        except Exception as e:
            print("[Lighter] Parse error:", e)

    def _on_error(self, ws, error):
        print("[Lighter] WS error:", error)

    def _on_close(self, ws, status, msg):
        print(f"[Lighter] WS closed {status}: {msg}")

    # -----------------------------------------------------------------
    def stop(self):
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except:
                pass

class MEXCWebSocketClient:
    """Client WebSocket chuẩn nhất cho MEXC Depth"""

    def __init__(self, symbol: str = "BTC_USDT"):
        self.symbol = symbol
        self.orderbook = OrderBook("MEXC")
        self.ws = None
        self.ws_url = "wss://contract.mexc.com/edge"
        self.running = False
        self.thread = None

    # ============================
    # START
    # ============================
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run_websocket)
        self.thread.daemon = True
        self.thread.start()

    # ============================
    # MAIN LOOP + AUTO RECONNECT
    # ============================
    def _run_websocket(self):
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )
                self.ws.run_forever(ping_interval=15, ping_timeout=8)

            except Exception as e:
                print("[MEXC] WS error:", e)

            print("[MEXC] Retry in 3s...")
            time.sleep(3)

    # ============================
    # ON OPEN
    # ============================
    def _on_open(self, ws):
        print("[MEXC] Connected, subscribing...")

        subscribe_msg = {
            "method": "sub.depth",
            "param": {"symbol": self.symbol}
        }

        ws.send(json.dumps(subscribe_msg))

        print("[MEXC] Fetching fresh snapshot...")

        # LẤY SNAPSHOT
        self._fetch_snapshot()

        print("[MEXC] Snapshot loaded ✓")

    # ============================
    # FETCH SNAPSHOT VIA REST
    # ============================
    def _fetch_snapshot(self):
        try:
            url = f"https://contract.mexc.com/api/v1/contract/depth/{self.symbol}"
            res = requests.get(url, timeout=5).json()

            asks = [[str(x[0]), str(x[1])] for x in res["data"]["asks"]]
            bids = [[str(x[0]), str(x[1])] for x in res["data"]["bids"]]

            # Gán trực tiếp snapshot
            self.orderbook.asks = asks
            self.orderbook.bids = bids
            self.orderbook.last_update = datetime.now()

        except Exception as e:
            print("[MEXC] Snapshot fetch failed:", e)

    # ============================
    # MESSAGE HANDLER
    # ============================
    def _on_message(self, ws, message):
        try:
            data = json.loads(message)

            if data.get("channel") == "push.depth":
                depth = data.get("data", {})

                new_asks = depth.get("asks", [])
                new_bids = depth.get("bids", [])

                if new_asks:
                    self._update_levels(self.orderbook.asks, new_asks, 'asc')

                if new_bids:
                    self._update_levels(self.orderbook.bids, new_bids, 'desc')

                self.orderbook.last_update = datetime.now()

        except Exception as e:
            print("[MEXC] parse error:", e)

    # ============================
    # UPDATE ORDERBOOK
    # ============================
    def _update_levels(self, current, updates, sort_order):
        for p, q, *_ in updates:
            price = float(p)
            qty = float(q)

            if qty == 0:
                current[:] = [l for l in current if float(l[0]) != price]
            else:
                updated = False
                for i, row in enumerate(current):
                    if float(row[0]) == price:
                        current[i] = [str(p), str(q)]
                        updated = True
                        break

                if not updated:
                    current.append([str(p), str(q)])

        reverse = (sort_order == "desc")
        current.sort(key=lambda x: float(x[0]), reverse=reverse)

    # ============================
    # ERROR + CLOSE
    # ============================
    def _on_error(self, ws, error):
        print("[MEXC] Error:", error)

    def _on_close(self, ws, code, msg):
        print("[MEXC] Closed:", code, msg)

    # ============================
    # STOP
    # ============================
    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()

class ArbitrageAnalyzer:
    """Phân tích cơ hội arbitrage giữa 2 sàn"""
    def __init__(self, lighter_client: LighterWebSocketClient, 
                 mexc_client: MEXCWebSocketClient,
                 telegram_notifier: Optional[TelegramNotifier] = None,
                 min_spread_usd: float = 10.0):
        self.lighter = lighter_client
        self.mexc = mexc_client
        self.telegram = telegram_notifier
        self.min_spread_usd = min_spread_usd  # Chênh lệch tối thiểu để gửi alert
        self.opportunities = deque(maxlen=100)
        self.last_print_time = None
        
    def analyze(self, trade_size: float = 0.1) -> Dict:
        """
        Phân tích chênh lệch giá và cơ hội hedge
        
        Args:
            trade_size: Kích thước giao dịch để test (BTC hoặc ETH)
        
        Returns:
            Dict chứa thông tin phân tích
        """
        lighter_ob = self.lighter.orderbook
        mexc_ob = self.mexc.orderbook
        
        # Kiểm tra orderbook có data chưa
        if not lighter_ob.get_best_bid() or not mexc_ob.get_best_bid():
            return {"status": "waiting", "message": "Đang chờ orderbook data..."}
        
        # Lấy best bid/ask
        lighter_bid = lighter_ob.get_best_bid()
        lighter_ask = lighter_ob.get_best_ask()
        mexc_bid = mexc_ob.get_best_bid()
        mexc_ask = mexc_ob.get_best_ask()
        
        # Kiểm tra data hợp lệ (không có giá = 0)
        if not lighter_ask or not mexc_ask or lighter_ask[0] == 0 or mexc_ask[0] == 0:
            return {"status": "waiting", "message": "Orderbook chưa đầy đủ..."}
        
        # Tính giá trung bình khi dùng lệnh market
        lighter_buy_price, _ = lighter_ob.calculate_market_impact('buy', trade_size)
        lighter_sell_price, _ = lighter_ob.calculate_market_impact('sell', trade_size)
        mexc_buy_price, _ = mexc_ob.calculate_market_impact('buy', trade_size)
        mexc_sell_price, _ = mexc_ob.calculate_market_impact('sell', trade_size)
        
        # Tính spread giữa 2 sàn
        # Cơ hội 1: Mua Lighter, Bán MEXC
        opportunity_1 = {
            "direction": "Buy Lighter → Sell MEXC",
            "buy_exchange": "Lighter",
            "buy_price": lighter_buy_price,
            "sell_exchange": "MEXC", 
            "sell_price": mexc_sell_price,
            "spread_pct": ((mexc_sell_price - lighter_buy_price) / lighter_buy_price * 100) 
                          if lighter_buy_price > 0 else 0,
            "profit_per_unit": mexc_sell_price - lighter_buy_price if lighter_buy_price > 0 else 0
        }
        
        # Cơ hội 2: Mua MEXC, Bán Lighter
        opportunity_2 = {
            "direction": "Buy MEXC → Sell Lighter",
            "buy_exchange": "MEXC",
            "buy_price": mexc_buy_price,
            "sell_exchange": "Lighter",
            "sell_price": lighter_sell_price,
            "spread_pct": ((lighter_sell_price - mexc_buy_price) / mexc_buy_price * 100)
                          if mexc_buy_price > 0 else 0,
            "profit_per_unit": lighter_sell_price - mexc_buy_price if mexc_buy_price > 0 else 0
        }
        
        result = {
            "timestamp": datetime.now().isoformat(),
            "trade_size": trade_size,
            "lighter": {
                "best_bid": lighter_bid[0] if lighter_bid else 0,
                "best_ask": lighter_ask[0] if lighter_ask else 0,
                "market_buy_price": lighter_buy_price,
                "market_sell_price": lighter_sell_price,
                "last_update": lighter_ob.last_update.strftime("%H:%M:%S.%f")[:-3] if lighter_ob.last_update else "N/A"
            },
            "mexc": {
                "best_bid": mexc_bid[0] if mexc_bid else 0,
                "best_ask": mexc_ask[0] if mexc_ask else 0,
                "market_buy_price": mexc_buy_price,
                "market_sell_price": mexc_sell_price,
                "last_update": mexc_ob.last_update.strftime("%H:%M:%S.%f")[:-3] if mexc_ob.last_update else "N/A"
            },
            "opportunities": [opportunity_1, opportunity_2]
        }
        
        # Lưu cơ hội nếu spread > 0.05% (có thể profitable sau khi trừ phí)
        if opportunity_1["spread_pct"] > 0.05 or opportunity_2["spread_pct"] > 0.05:
            self.opportunities.append(result)
        
        return result
    
    async def check_and_notify(self, result: Dict, unit_name: str):
        """
        Kiểm tra và gửi notification nếu có cơ hội tốt
        
        Args:
            result: Kết quả phân tích
            unit_name: Tên đơn vị (BTC, ETH)
        """
        if not self.telegram or result.get("status") == "waiting":
            return
        
        for opp in result['opportunities']:
            if opp['profit_per_unit'] >= self.min_spread_usd:
                await self.telegram.send_arbitrage_alert(
                    opp, 
                    result['trade_size'],
                    unit_name
                )
    
    def print_analysis(self, result: Dict, compact: bool = False):
        """
        In ra kết quả phân tích
        
        Args:
            result: Kết quả phân tích
            compact: Nếu True, chỉ in 1 dòng update
        """
        if result.get("status") == "waiting":
            print(f"\r{result['message']}", end="", flush=True)
            return
        
        if compact:
            # In 1 dòng compact cho update nhanh
            lighter = result['lighter']
            mexc = result['mexc']
            
            # Tính chênh lệch giá tuyệt đối
            spread_lighter_buy_mexc_sell = mexc['best_bid'] - lighter['best_ask']
            spread_mexc_buy_lighter_sell = lighter['best_bid'] - mexc['best_ask']
            
            # Màu sắc cho profit
            if spread_lighter_buy_mexc_sell > 0:
                color1 = "🟢"
                spread1_str = f"+${abs(spread_lighter_buy_mexc_sell):,.2f}"
            else:
                color1 = "🔴"
                spread1_str = f"-${abs(spread_lighter_buy_mexc_sell):,.2f}"
            
            if spread_mexc_buy_lighter_sell > 0:
                color2 = "🟢"
                spread2_str = f"+${abs(spread_mexc_buy_lighter_sell):,.2f}"
            else:
                color2 = "🔴"
                spread2_str = f"-${abs(spread_mexc_buy_lighter_sell):,.2f}"
            
            print(f"\r[{datetime.now().strftime('%H:%M:%S.%f')[:-4]}] "
                  f"Lighter: ${lighter['best_ask']:,.2f}↑ ${lighter['best_bid']:,.2f}↓ | "
                  f"MEXC: ${mexc['best_ask']:,.2f}↑ ${mexc['best_bid']:,.2f}↓ | "
                  f"{color1} L→M: {spread1_str} | "
                  f"{color2} M→L: {spread2_str}",
                  end="", flush=True)
        else:
            # In chi tiết đầy đủ
            print("\n" + "="*80)
            print(f"ARBITRAGE ANALYSIS - {result['timestamp']}")
            print(f"Trade Size: {result['trade_size']} units")
            print("="*80)
            
            # In orderbook info
            print("\n📊 ORDERBOOK INFO:")
            print(f"\n  Lighter (Last: {result['lighter']['last_update']}):")
            print(f"    Best Bid: ${result['lighter']['best_bid']:,.2f}")
            print(f"    Best Ask: ${result['lighter']['best_ask']:,.2f}")
            print(f"    Market Buy:  ${result['lighter']['market_buy_price']:,.2f}")
            print(f"    Market Sell: ${result['lighter']['market_sell_price']:,.2f}")
            
            print(f"\n  MEXC (Last: {result['mexc']['last_update']}):")
            print(f"    Best Bid: ${result['mexc']['best_bid']:,.2f}")
            print(f"    Best Ask: ${result['mexc']['best_ask']:,.2f}")
            print(f"    Market Buy:  ${result['mexc']['market_buy_price']:,.2f}")
            print(f"    Market Sell: ${result['mexc']['market_sell_price']:,.2f}")
            
            # In opportunities
            print("\n💰 ARBITRAGE OPPORTUNITIES:")
            for i, opp in enumerate(result['opportunities'], 1):
                spread_abs = abs(opp['profit_per_unit'])
                print(f"\n  Opportunity {i}: {opp['direction']}")
                print(f"    Buy  @ {opp['buy_exchange']:7s}: ${opp['buy_price']:,.2f}")
                print(f"    Sell @ {opp['sell_exchange']:7s}: ${opp['sell_price']:,.2f}")
                print(f"    Chênh lệch: {opp['profit_per_unit']:+,.2f} USD ({opp['spread_pct']:+.4f}%)")
                print(f"    Profit nếu {result['trade_size']} units: ${opp['profit_per_unit'] * result['trade_size']:+,.2f}")
                
                if opp['profit_per_unit'] > 10:
                    print(f"    ✅ PROFITABLE! Chênh ${spread_abs:,.2f}")
                elif opp['profit_per_unit'] > 2:
                    print(f"    ⚠️  Nhỏ - Cân nhắc phí ({spread_abs:,.2f} USD)")
                else:
                    print(f"    ❌ Không có lợi nhuận ({spread_abs:,.2f} USD)")
            
            print("\n" + "="*80)


async def main():
    """Main function"""
    print("🚀 Starting Lighter vs MEXC Arbitrage Bot (WebSocket Mode)")
    print("=" * 80)
    
    # Load config
    telegram_notifier = None
    
    if TELEGRAM_ENABLED:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
            print("\n⚠️  Telegram enabled nhưng thiếu Bot Token hoặc Chat ID!")
            print("   → Vui lòng sửa TELEGRAM_BOT_TOKEN và TELEGRAM_CHAT_ID trong code")
            print("   → Hoặc đặt TELEGRAM_ENABLED = False để tắt Telegram\n")
        else:
            telegram_notifier = TelegramNotifier(
                TELEGRAM_BOT_TOKEN, 
                TELEGRAM_CHAT_IDS,
                cooldown_seconds=ALERT_COOLDOWN
            )
            
            # Test connection
            print("\n🔄 Testing Telegram connection...")
            test_success = await telegram_notifier.send_message(
                "✅ <b>Arbitrage Bot Started!</b>\n\n"
                f"Min spread alert: <b>${MIN_SPREAD_USD:,.2f}</b>\n"
                f"Cooldown: <b>{ALERT_COOLDOWN}s</b>"
            )
            
            if test_success:
                print(f"✅ Telegram connected! Alert threshold: ${MIN_SPREAD_USD:,.2f}")
            else:
                print("❌ Telegram connection failed! Continuing without alerts...")
                telegram_notifier = None
    
    # Cấu hình market
    if MARKET_CHOICE.upper() == "ETH":
        lighter_market_index = 0
        mexc_symbol = "ETH_USDT"
        trade_size = TRADE_SIZE if TRADE_SIZE > 0 else 1.0
        unit_name = "ETH"
    else:  # Default BTC
        lighter_market_index = 1
        mexc_symbol = "BTC_USDT"
        trade_size = TRADE_SIZE if TRADE_SIZE > 0 else 0.1
        unit_name = "BTC"
    
    print(f"\n✅ Configuration:")
    print(f"   - Market: {unit_name}")
    print(f"   - Lighter: market_index={lighter_market_index}")
    print(f"   - MEXC: {mexc_symbol}")
    print(f"   - Trade size: {trade_size} {unit_name}")
    print(f"   - Update interval: {UPDATE_INTERVAL}s")
    if telegram_notifier:
        print(f"   - Telegram: ✅ Enabled (min: ${MIN_SPREAD_USD:,.2f}, cooldown: {ALERT_COOLDOWN}s)")
    else:
        print(f"   - Telegram: ❌ Disabled")
    
    # Khởi tạo clients
    lighter_client = LighterWebSocketClient(market_index=lighter_market_index)
    mexc_client = MEXCWebSocketClient(symbol=mexc_symbol)
    
    # Start WebSocket connections
    print("\n🔌 Connecting to exchanges...")
    lighter_client.start()
    mexc_client.start()
    
    # Chờ kết nối và nhận data
    print("⏳ Waiting for orderbook data...")
    await asyncio.sleep(5)
    
    # Khởi tạo analyzer
    analyzer = ArbitrageAnalyzer(
        lighter_client, 
        mexc_client,
        telegram_notifier=telegram_notifier,
        min_spread_usd=MIN_SPREAD_USD
    )
    
    print("\n✅ Bot started! Analyzing arbitrage opportunities...")
    print("   - Press Ctrl+C to stop")
    print(f"   - Updates every {UPDATE_INTERVAL}s (real-time)")
    print("   - 🟢 = Profitable | 🔴 = Not profitable")
    print("\nLegend: L→M = Buy Lighter, Sell MEXC | M→L = Buy MEXC, Sell Lighter")
    print("=" * 100)
    
    try:
        while True:
            result = analyzer.analyze(trade_size=trade_size)
            
            # Chỉ in compact mode
            analyzer.print_analysis(result, compact=True)
            
            # Check và gửi Telegram alert nếu có cơ hội
            await analyzer.check_and_notify(result, unit_name)
            
            await asyncio.sleep(UPDATE_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopping bot...")
        lighter_client.stop()
        mexc_client.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:

        print("\n👋 Goodbye!")
